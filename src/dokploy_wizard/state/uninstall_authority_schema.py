"""Schema types and parsing for receipt-bound uninstall authority."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.state.models import OwnedResource

_FORMAT_VERSION = 1


class UninstallAuthorityError(RuntimeError):
    """Raised when persisted uninstall authority is malformed or conflicts."""


@dataclass(frozen=True, slots=True)
class UninstallAuthority:
    """Exact provider target that a creation receipt authorizes for deletion."""

    resource_type: str
    resource_id: str
    scope: str
    owner_id: str
    provider: str
    physical_target_id: str
    parent_target_id: str
    expected_fingerprint: str

    def __post_init__(self) -> None:
        if any(
            value == ""
            for value in (
                self.resource_type,
                self.resource_id,
                self.scope,
                self.owner_id,
                self.provider,
                self.physical_target_id,
                self.parent_target_id,
                self.expected_fingerprint,
            )
        ):
            raise UninstallAuthorityError("Uninstall authority fields must be non-empty.")

    def matches(self, resource: OwnedResource) -> bool:
        return resource_key(self) == resource_key(resource)

    def to_dict(self) -> dict[str, str]:
        return {
            "expected_fingerprint": self.expected_fingerprint,
            "owner_id": self.owner_id,
            "parent_target_id": self.parent_target_id,
            "physical_target_id": self.physical_target_id,
            "provider": self.provider,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> UninstallAuthority:
        require_exact_keys(
            payload,
            {
                "expected_fingerprint",
                "owner_id",
                "parent_target_id",
                "physical_target_id",
                "provider",
                "resource_id",
                "resource_type",
                "scope",
            },
        )
        return cls(
            expected_fingerprint=require_string(payload, "expected_fingerprint"),
            owner_id=require_string(payload, "owner_id"),
            parent_target_id=require_string(payload, "parent_target_id"),
            physical_target_id=require_string(payload, "physical_target_id"),
            provider=require_string(payload, "provider"),
            resource_id=require_string(payload, "resource_id"),
            resource_type=require_string(payload, "resource_type"),
            scope=require_string(payload, "scope"),
        )


@dataclass(frozen=True, slots=True)
class UninstallDeletionReceipt:
    """Durable proof that one authorized provider target was re-read absent."""

    resource_type: str
    resource_id: str
    scope: str
    expected_fingerprint: str

    def matches(self, resource: OwnedResource) -> bool:
        return resource_key(self) == resource_key(resource)

    def to_dict(self) -> dict[str, str]:
        return {
            "expected_fingerprint": self.expected_fingerprint,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> UninstallDeletionReceipt:
        require_exact_keys(
            payload,
            {"expected_fingerprint", "resource_id", "resource_type", "scope"},
        )
        return cls(
            expected_fingerprint=require_string(payload, "expected_fingerprint"),
            resource_id=require_string(payload, "resource_id"),
            resource_type=require_string(payload, "resource_type"),
            scope=require_string(payload, "scope"),
        )


@dataclass(frozen=True, slots=True)
class AuthorityDocument:
    """Entire persisted uninstall-authority document."""

    authorities: tuple[UninstallAuthority, ...]
    deletions: tuple[UninstallDeletionReceipt, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "authorities": [authority.to_dict() for authority in self.authorities],
            "deletions": [receipt.to_dict() for receipt in self.deletions],
            "format_version": _FORMAT_VERSION,
        }


def parse_document(payload: dict[str, object]) -> AuthorityDocument:
    """Parse the exact persisted uninstall-authority schema."""

    require_exact_keys(payload, {"authorities", "deletions", "format_version"})
    if payload["format_version"] != _FORMAT_VERSION:
        raise UninstallAuthorityError("Uninstall authority format version is unsupported.")
    authorities = parse_authorities(payload["authorities"])
    deletions = parse_deletions(payload["deletions"])
    require_unique_authorities(authorities)
    require_unique_deletions(deletions)
    return AuthorityDocument(authorities=authorities, deletions=deletions)


def empty_document() -> AuthorityDocument:
    """Return the empty authority document."""

    return AuthorityDocument((), ())


def find_authority(
    authorities: tuple[UninstallAuthority, ...], resource: OwnedResource
) -> UninstallAuthority | None:
    return next((authority for authority in authorities if authority.matches(resource)), None)


def find_deletion(
    deletions: tuple[UninstallDeletionReceipt, ...], resource: OwnedResource
) -> UninstallDeletionReceipt | None:
    return next((receipt for receipt in deletions if receipt.matches(resource)), None)


def sorted_authorities(
    authorities: tuple[UninstallAuthority, ...],
) -> tuple[UninstallAuthority, ...]:
    return tuple(sorted(authorities, key=resource_key))


def sorted_deletions(
    deletions: tuple[UninstallDeletionReceipt, ...],
) -> tuple[UninstallDeletionReceipt, ...]:
    return tuple(sorted(deletions, key=resource_key))


def parse_authorities(value: object) -> tuple[UninstallAuthority, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise UninstallAuthorityError("Uninstall authorities must be a list of objects.")
    return tuple(UninstallAuthority.from_dict(item) for item in value)


def parse_deletions(value: object) -> tuple[UninstallDeletionReceipt, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise UninstallAuthorityError("Uninstall deletion receipts must be a list of objects.")
    return tuple(UninstallDeletionReceipt.from_dict(item) for item in value)


def require_exact_keys(payload: dict[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        raise UninstallAuthorityError("Uninstall authority document has an invalid schema.")


def require_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise UninstallAuthorityError(f"Uninstall authority field '{key}' must be non-empty.")
    return value


def resource_key(
    resource: OwnedResource | UninstallAuthority | UninstallDeletionReceipt,
) -> tuple[str, str, str]:
    return (resource.resource_type, resource.resource_id, resource.scope)


def require_unique_authorities(authorities: tuple[UninstallAuthority, ...]) -> None:
    if len({resource_key(authority) for authority in authorities}) != len(authorities):
        raise UninstallAuthorityError("Uninstall authorities contain a duplicate resource.")


def require_unique_deletions(deletions: tuple[UninstallDeletionReceipt, ...]) -> None:
    if len({resource_key(receipt) for receipt in deletions}) != len(deletions):
        raise UninstallAuthorityError("Uninstall deletion receipts contain a duplicate resource.")
