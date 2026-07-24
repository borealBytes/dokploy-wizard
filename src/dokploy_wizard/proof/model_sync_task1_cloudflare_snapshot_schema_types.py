"""Typed redacted Cloudflare resource and pagination projections."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from dokploy_wizard.proof.model_sync_artifacts import JsonValue

SNAPSHOT_SCHEMA_VERSION: Final = 1
_HASH: Final = re.compile(r"^[a-f0-9]{64}$")
_TEXT: Final = re.compile(r"^[\x21-\x7e]{1,128}$")
KINDS: Final = (
    "access_application",
    "access_policy",
    "certificate_pack",
    "dns_record",
    "identity_provider",
    "tunnel",
)
FIELDS: Final = {
    "access_application": frozenset(
        {
            "allowed_identity_provider_sha256",
            "app_type",
            "domain_sha256",
            "name_sha256",
            "spec_sha256",
        }
    ),
    "access_policy": frozenset(
        {"app_id_sha256", "decision", "email_sha256", "name_sha256", "spec_sha256"}
    ),
    "certificate_pack": frozenset({"host_sha256", "pack_type", "spec_sha256", "status"}),
    "dns_record": frozenset(
        {"content_sha256", "name_sha256", "proxied", "record_type", "spec_sha256"}
    ),
    "identity_provider": frozenset({"name_sha256", "provider_type", "spec_sha256"}),
    "tunnel": frozenset(
        {
            "config_src",
            "configuration_availability",
            "configuration_sha256",
            "name_sha256",
            "spec_sha256",
            "status",
        }
    ),
}
LIST_FIELDS: Final = frozenset({"allowed_identity_provider_sha256", "email_sha256", "host_sha256"})
HASH_FIELDS: Final = frozenset(
    {
        "app_id_sha256",
        "configuration_sha256",
        "content_sha256",
        "domain_sha256",
        "name_sha256",
        "spec_sha256",
    }
)
OTP_NAME_SHA256: Final = hashlib.sha256(b"One-time PIN login").hexdigest()


class CloudflareSnapshotError(ValueError):
    """Raised when a Cloudflare snapshot is incomplete, noncanonical, or unsafe."""


def canonical_snapshot_bytes(value: Mapping[str, JsonValue]) -> bytes:
    """Encode a compact deterministic redacted snapshot value."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    )


def require_snapshot_hash(value: str) -> str:
    """Validate a nonzero lower-case SHA-256 projection."""
    if _HASH.fullmatch(value) is None or value == "0" * 64:
        raise CloudflareSnapshotError("snapshot hash is invalid")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or _TEXT.fullmatch(value) is None:
        raise CloudflareSnapshotError("snapshot text is invalid")
    return value


def _hashes(value: JsonValue) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CloudflareSnapshotError("snapshot hash list is invalid")
    hashes = tuple(
        require_snapshot_hash(item) if isinstance(item, str) else require_snapshot_hash("")
        for item in value
    )
    if hashes != tuple(sorted(set(hashes))):
        raise CloudflareSnapshotError("snapshot hash list is noncanonical")
    return hashes


@dataclass(frozen=True, slots=True)
class CloudflareSnapshotResourceV1:
    """One complete redacted account or zone resource projection."""

    kind: str
    identity_sha256: str
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise CloudflareSnapshotError("snapshot resource kind is unsupported")
        require_snapshot_hash(self.identity_sha256)
        if set(self.payload) != FIELDS[self.kind]:
            raise CloudflareSnapshotError("snapshot resource keys are invalid")
        for key, value in self.payload.items():
            if key in LIST_FIELDS:
                _hashes(value)
            elif key in HASH_FIELDS:
                require_snapshot_hash(value if isinstance(value, str) else "")
            elif key == "proxied":
                if not isinstance(value, bool):
                    raise CloudflareSnapshotError("snapshot proxied flag is invalid")
            else:
                _text(value)

    @property
    def semantic_sha256(self) -> str:
        return hashlib.sha256(canonical_snapshot_bytes(self.to_payload())).hexdigest()

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "identity_sha256": self.identity_sha256,
            "kind": self.kind,
            "payload": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in sorted(self.payload.items())
            },
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, JsonValue]) -> "CloudflareSnapshotResourceV1":
        if set(value) != {"identity_sha256", "kind", "payload"}:
            raise CloudflareSnapshotError("snapshot resource keys are invalid")
        kind = _text(value["kind"])
        payload = value["payload"]
        if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
            raise CloudflareSnapshotError("snapshot resource payload is invalid")
        normalized: dict[str, JsonValue] = {}
        for key, item in payload.items():
            normalized[key] = list(_hashes(item)) if key in LIST_FIELDS else item
        identifier = value["identity_sha256"]
        return cls(
            kind,
            require_snapshot_hash(identifier if isinstance(identifier, str) else ""),
            normalized,
        )


@dataclass(frozen=True, slots=True)
class CloudflareSnapshotCollectionV1:
    """Validated pagination completeness metadata for one resource kind."""

    kind: str
    page_count: int
    per_page: int
    total_count: int

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise CloudflareSnapshotError("snapshot collection kind is unsupported")
        values = (self.page_count, self.total_count)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values
            )
            or not 1 <= self.per_page <= 100
        ):
            raise CloudflareSnapshotError("snapshot collection metadata is invalid")
        if self.kind == "access_policy":
            if (self.total_count == 0) != (self.page_count == 0):
                raise CloudflareSnapshotError("snapshot policy pagination is invalid")
        elif self.page_count < 1 or self.page_count != max(
            1, (self.total_count + self.per_page - 1) // self.per_page
        ):
            raise CloudflareSnapshotError("snapshot collection pagination is invalid")

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "page_count": self.page_count,
            "per_page": self.per_page,
            "total_count": self.total_count,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, JsonValue]) -> "CloudflareSnapshotCollectionV1":
        if set(value) != {"kind", "page_count", "per_page", "total_count"}:
            raise CloudflareSnapshotError("snapshot collection keys are invalid")
        return cls(
            _text(value["kind"]),
            _int(value["page_count"]),
            _int(value["per_page"]),
            _int(value["total_count"]),
        )


def _int(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudflareSnapshotError("snapshot collection metadata is invalid")
    return value
