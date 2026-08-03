"""Closed create provenance used to grant uninstall authority."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dokploy_wizard.state.models import OwnedResource
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_authority_schema import (
    UninstallAuthority,
    UninstallAuthorityError,
)


class ProviderCreationDisposition(StrEnum):
    """The provider action that produced a resource observation."""

    CREATED = "created"
    REUSED = "reused"
    UPDATED = "updated"


@dataclass(frozen=True, slots=True)
class ProviderCreationResult:
    """Exact provider observation associated with one logical resource."""

    disposition: ProviderCreationDisposition
    resource: OwnedResource
    owner_id: str
    provider: str
    physical_target_id: str
    parent_target_id: str
    expected_fingerprint: str

    def __post_init__(self) -> None:
        if self.owner_id == "" or self.provider == "":
            raise UninstallAuthorityError(
                "Provider creation result must bind an owner and provider."
            )
        if self.physical_target_id == "" or self.parent_target_id == "":
            raise UninstallAuthorityError(
                "Provider creation result must bind exact physical targets."
            )
        if len(self.expected_fingerprint) != 64:
            raise UninstallAuthorityError(
                "Provider creation result fingerprint must be a SHA-256 digest."
            )
        if any(character not in "0123456789abcdef" for character in self.expected_fingerprint):
            raise UninstallAuthorityError(
                "Provider creation result fingerprint must be lowercase hexadecimal."
            )


def publish_created_authority(
    store: UninstallAuthorityStore, result: ProviderCreationResult
) -> UninstallAuthority | None:
    """Persist deletion authority only for a verified provider creation."""

    match result.disposition:
        case ProviderCreationDisposition.CREATED:
            return store.record_created(
                resource=result.resource,
                owner_id=result.owner_id,
                provider=result.provider,
                physical_target_id=result.physical_target_id,
                parent_target_id=result.parent_target_id,
                expected_fingerprint=result.expected_fingerprint,
            )
        case ProviderCreationDisposition.REUSED | ProviderCreationDisposition.UPDATED:
            return None
