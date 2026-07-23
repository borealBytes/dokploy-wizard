"""Typed, redacted Cloudflare preservation evidence for Task 1."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from dokploy_wizard.proof import canonical_json_bytes
from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    require_keys,
    require_mapping,
    require_sha256,
    require_text,
)

CloudflareProvenance = Literal["preexisting_unowned", "created_candidate", "foreign"]
CloudflareMatch = Literal["exact", "foreign"]
_SUPPORTED_KINDS = frozenset(
    {"dns_record", "tunnel", "hostname_route", "access_application", "access_policy"}
)


@dataclass(frozen=True, slots=True)
class CloudflareResource:
    resource_id: str
    kind: str
    fingerprint_sha256: str
    match: CloudflareMatch
    provenance: CloudflareProvenance

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "fingerprint_sha256": self.fingerprint_sha256,
            "id": self.resource_id,
            "kind": self.kind,
            "match": self.match,
            "provenance": self.provenance,
        }


def classify_cloudflare_preflight(
    values: Sequence[JsonValue],
    deterministic_names: set[str],
    stack_name: str,
) -> tuple[CloudflareResource, ...]:
    """Accept only exact supported deterministic names as preserve-only evidence."""
    parsed = _parse_resources(values, deterministic_names, stack_name)
    return tuple(
        CloudflareResource(
            item.resource_id, item.kind, item.fingerprint_sha256, item.match, "preexisting_unowned"
        )
        if item.match == "exact"
        else item
        for item in parsed
    )


def classify_cloudflare_post_install(
    values: Sequence[JsonValue],
    deterministic_names: set[str],
    stack_name: str,
    before: tuple[CloudflareResource, ...],
) -> tuple[CloudflareResource, ...]:
    """Preserve exact preimages and mark only new exact objects as candidates."""
    parsed = _parse_resources(values, deterministic_names, stack_name)
    preexisting = {
        (item.kind, item.resource_id): item
        for item in before
        if item.provenance == "preexisting_unowned"
    }
    observed = {(item.kind, item.resource_id): item for item in parsed}
    if set(observed) != {(item.kind, item.resource_id) for item in parsed}:
        raise CaptureSchemaError("Cloudflare post-install resources have duplicate identities")
    for identity, previous in preexisting.items():
        current = observed.get(identity)
        if current is None:
            raise CaptureSchemaError("preexisting Cloudflare resource is missing after install")
        if current.match != "exact":
            raise CaptureSchemaError("preexisting Cloudflare deterministic match changed")
        if current.fingerprint_sha256 != previous.fingerprint_sha256:
            raise CaptureSchemaError("preexisting Cloudflare fingerprint changed after install")
    return tuple(
        CloudflareResource(
            item.resource_id, item.kind, item.fingerprint_sha256, item.match, "preexisting_unowned"
        )
        if (item.kind, item.resource_id) in preexisting
        else CloudflareResource(
            item.resource_id, item.kind, item.fingerprint_sha256, item.match, "created_candidate"
        )
        if item.match == "exact"
        else item
        for item in parsed
    )


def verify_preexisting_cloudflare(
    before: tuple[CloudflareResource, ...], after: tuple[CloudflareResource, ...]
) -> tuple[CloudflareResource, ...]:
    """Reject missing, renamed, or mutated preserve-only objects after installation."""
    preexisting = {
        (item.kind, item.resource_id): item
        for item in before
        if item.provenance == "preexisting_unowned"
    }
    observed = {(item.kind, item.resource_id): item for item in after}
    for identity, previous in preexisting.items():
        current = observed.get(identity)
        if current is None:
            raise CaptureSchemaError("preexisting Cloudflare resource is missing after install")
        if current.match != "exact":
            raise CaptureSchemaError("preexisting Cloudflare deterministic match changed")
        if current.fingerprint_sha256 != previous.fingerprint_sha256:
            raise CaptureSchemaError("preexisting Cloudflare fingerprint changed after install")
    return tuple(
        CloudflareResource(
            item.resource_id,
            item.kind,
            item.fingerprint_sha256,
            item.match,
            "preexisting_unowned"
            if (item.kind, item.resource_id) in preexisting
            else "created_candidate",
        )
        if item.match == "exact"
        else item
        for item in after
    )


def preexisting_cloudflare_sha256(resources: tuple[CloudflareResource, ...]) -> str:
    """Hash the canonical redacted preserve-only snapshot."""
    payload = [
        item.to_dict()
        for item in sorted(resources, key=lambda value: (value.kind, value.resource_id))
        if item.provenance == "preexisting_unowned"
    ]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def cloudflare_evidence_payload(resources: tuple[CloudflareResource, ...]) -> list[JsonValue]:
    """Serialize redacted Cloudflare evidence without raw API fields."""
    return [
        item.to_dict()
        for item in sorted(resources, key=lambda value: (value.kind, value.resource_id))
    ]


def cloudflare_snapshot_sha256(resources: tuple[CloudflareResource, ...]) -> str:
    """Hash every canonical redacted Cloudflare record, regardless of provenance."""
    return hashlib.sha256(canonical_json_bytes(cloudflare_evidence_payload(resources))).hexdigest()


def _parse_resources(
    values: Sequence[JsonValue], deterministic_names: set[str], stack_name: str
) -> tuple[CloudflareResource, ...]:
    resources: list[CloudflareResource] = []
    exact_names: set[tuple[str, str]] = set()
    identities: set[tuple[str, str]] = set()
    resource_ids: set[str] = set()
    for value in values:
        source = require_mapping(value, "Cloudflare resource")
        require_keys(source, {"fingerprint_sha256", "id", "kind", "name"}, "Cloudflare resource")
        resource_id = require_text(source["id"], "Cloudflare resource id")
        kind = require_text(source["kind"], "Cloudflare resource kind")
        name = require_text(source["name"], "Cloudflare resource name")
        fingerprint = require_sha256(
            source["fingerprint_sha256"], "Cloudflare resource fingerprint"
        )
        identity = (kind, resource_id)
        if identity in identities or resource_id in resource_ids:
            raise CaptureSchemaError("Cloudflare resources have duplicate identities")
        identities.add(identity)
        resource_ids.add(resource_id)
        if name.startswith(f"{stack_name}-") and name not in deterministic_names:
            raise CaptureSchemaError("Cloudflare stack-prefix resource is ambiguous")
        match: CloudflareMatch = "exact" if name in deterministic_names else "foreign"
        if match == "exact":
            if kind not in _SUPPORTED_KINDS:
                raise CaptureSchemaError("Cloudflare exact resource kind is unsupported")
            exact_identity = (kind, name)
            if exact_identity in exact_names:
                raise CaptureSchemaError("Cloudflare exact deterministic resource is duplicated")
            exact_names.add(exact_identity)
        resources.append(CloudflareResource(resource_id, kind, fingerprint, match, "foreign"))
    return tuple(sorted(resources, key=lambda item: (item.kind, item.resource_id)))
