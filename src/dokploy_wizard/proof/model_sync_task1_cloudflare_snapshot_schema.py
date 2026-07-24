"""Canonical complete redacted Cloudflare snapshot schema for Task 1."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema_types import (
    KINDS,
    OTP_NAME_SHA256,
    SNAPSHOT_SCHEMA_VERSION,
    CloudflareSnapshotCollectionV1,
    CloudflareSnapshotError,
    CloudflareSnapshotResourceV1,
    canonical_snapshot_bytes,
    require_snapshot_hash,
)

__all__ = (
    "CloudflareSnapshotCollectionV1",
    "CloudflareSnapshotError",
    "CloudflareSnapshotResourceV1",
    "CloudflareSnapshotV1",
)


@dataclass(frozen=True, slots=True)
class CloudflareSnapshotV1:
    """Canonical complete redacted Task 1 Cloudflare inventory."""

    context_sha256: str
    account_id_sha256: str
    zone_id_sha256: str
    collections: tuple[CloudflareSnapshotCollectionV1, ...]
    resources: tuple[CloudflareSnapshotResourceV1, ...]
    otp_provider_sha256: str
    snapshot_sha256: str

    @classmethod
    def create(
        cls,
        *,
        context_sha256: str,
        account_id_sha256: str,
        zone_id_sha256: str,
        collections: tuple[CloudflareSnapshotCollectionV1, ...],
        resources: tuple[CloudflareSnapshotResourceV1, ...],
        otp_provider_sha256: str,
    ) -> "CloudflareSnapshotV1":
        draft = cls(
            require_snapshot_hash(context_sha256),
            require_snapshot_hash(account_id_sha256),
            require_snapshot_hash(zone_id_sha256),
            tuple(sorted(collections, key=lambda item: item.kind)),
            tuple(sorted(resources, key=lambda item: (item.kind, item.identity_sha256))),
            require_snapshot_hash(otp_provider_sha256),
            "1" * 64,
        )
        draft._validate()
        return cls(
            draft.context_sha256,
            draft.account_id_sha256,
            draft.zone_id_sha256,
            draft.collections,
            draft.resources,
            draft.otp_provider_sha256,
            hashlib.sha256(canonical_snapshot_bytes(draft._core_payload())).hexdigest(),
        )

    def _core_payload(self) -> dict[str, JsonValue]:
        return {
            "account_id_sha256": self.account_id_sha256,
            "collections": [item.to_payload() for item in self.collections],
            "context_sha256": self.context_sha256,
            "otp_provider_sha256": self.otp_provider_sha256,
            "resources": [item.to_payload() for item in self.resources],
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "zone_id_sha256": self.zone_id_sha256,
        }

    def to_bytes(self) -> bytes:
        self._validate()
        return canonical_snapshot_bytes(
            {**self._core_payload(), "snapshot_sha256": self.snapshot_sha256}
        )

    @classmethod
    def from_bytes(cls, content: bytes) -> "CloudflareSnapshotV1":
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CloudflareSnapshotError("snapshot bytes are invalid") from error
        expected = {
            "account_id_sha256",
            "collections",
            "context_sha256",
            "otp_provider_sha256",
            "resources",
            "schema_version",
            "snapshot_sha256",
            "zone_id_sha256",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value["schema_version"] != SNAPSHOT_SCHEMA_VERSION
        ):
            raise CloudflareSnapshotError("snapshot schema is invalid")
        collections, resources = value["collections"], value["resources"]
        if not isinstance(collections, list) or not isinstance(resources, list):
            raise CloudflareSnapshotError("snapshot arrays are invalid")
        parsed_collections = tuple(
            CloudflareSnapshotCollectionV1.from_payload(item)
            for item in collections
            if isinstance(item, Mapping)
        )
        parsed_resources = tuple(
            CloudflareSnapshotResourceV1.from_payload(item)
            for item in resources
            if isinstance(item, Mapping)
        )
        if len(parsed_collections) != len(collections) or len(parsed_resources) != len(resources):
            raise CloudflareSnapshotError("snapshot entry is malformed")
        snapshot = cls(
            _value_hash(value, "context_sha256"),
            _value_hash(value, "account_id_sha256"),
            _value_hash(value, "zone_id_sha256"),
            parsed_collections,
            parsed_resources,
            _value_hash(value, "otp_provider_sha256"),
            _value_hash(value, "snapshot_sha256"),
        )
        snapshot._validate()
        if (
            snapshot.snapshot_sha256
            != hashlib.sha256(canonical_snapshot_bytes(snapshot._core_payload())).hexdigest()
        ):
            raise CloudflareSnapshotError("snapshot hash is invalid")
        if content != snapshot.to_bytes():
            raise CloudflareSnapshotError("snapshot bytes are noncanonical")
        return snapshot

    def _validate(self) -> None:
        if self.collections != tuple(
            sorted(self.collections, key=lambda item: item.kind)
        ) or self.resources != tuple(
            sorted(self.resources, key=lambda item: (item.kind, item.identity_sha256))
        ):
            raise CloudflareSnapshotError("snapshot entries are noncanonical")
        if len({item.kind for item in self.collections}) != len(self.collections) or len(
            {(item.kind, item.identity_sha256) for item in self.resources}
        ) != len(self.resources):
            raise CloudflareSnapshotError("snapshot entries are duplicated")
        counts = {item.kind: item.total_count for item in self.collections}
        if set(counts) != set(KINDS) or len(counts) != len(KINDS):
            raise CloudflareSnapshotError("snapshot collections are incomplete")
        observed = {kind: sum(item.kind == kind for item in self.resources) for kind in KINDS}
        if counts != observed:
            raise CloudflareSnapshotError("snapshot collection count is incomplete")
        otp = tuple(
            item
            for item in self.resources
            if item.kind == "identity_provider"
            and item.payload["name_sha256"] == OTP_NAME_SHA256
            and item.payload["provider_type"] == "onetimepin"
        )
        if len(otp) != 1 or otp[0].semantic_sha256 != self.otp_provider_sha256:
            raise CloudflareSnapshotError("snapshot OTP provider is invalid")


def _value_hash(value: Mapping[str, JsonValue], key: str) -> str:
    item = value[key]
    return require_snapshot_hash(item if isinstance(item, str) else "")
