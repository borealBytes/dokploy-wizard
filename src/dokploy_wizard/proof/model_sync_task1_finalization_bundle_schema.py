"""Strict canonical parser and serializer for Task 1 finalization recovery bundles."""

from __future__ import annotations

import base64
import json
import re
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_artifacts import JsonValue, require_mapping
from dokploy_wizard.proof.model_sync_state import AbortGuardError
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceV1,
)
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    FinalizationBundlePhase,
    Task1FinalizationBundle,
)

_PAYLOAD_NAMES: Final = frozenset(
    {
        "baseline.json",
        "host-a-preflight.json",
        "host-b-preflight.json",
        "single-host-lifecycle-baseline.json",
    }
)
_IMAGE_NAMES: Final = frozenset({"coder", "litellm", "pgvector", "postfix", "redis"})
_REQUIRED_IMAGE_NAMES: Final = frozenset({"coder", "litellm", "pgvector"})
_HASH = re.compile(r"[a-f0-9]{64}")
_COMMIT = re.compile(r"[a-f0-9]{40}")


def serialize_bundle(bundle: Task1FinalizationBundle) -> bytes:
    """Return one strict canonical v2 bundle representation."""
    _validate_bundle(bundle)
    payload = {
        "coder_secret_inventory_sha256": bundle.coder_secret_inventory_sha256,
        "context_sha256": bundle.context_sha256,
        "guard_id": bundle.guard_id,
        "host_identity_mode": bundle.host_identity_mode,
        "host_sha256": bundle.host_sha256,
        "images": dict(sorted(bundle.images.items())),
        "legacy_workspace_managed_fingerprints_sha256": (
            bundle.legacy_workspace_managed_fingerprints_sha256
        ),
        "payload_sha256": {
            name: artifacts.sha256_bytes(content)
            for name, content in sorted(bundle.payloads.items())
        },
        "payloads": {
            name: base64.b64encode(content).decode("ascii")
            for name, content in sorted(bundle.payloads.items())
        },
        "phase": str(bundle.phase),
        "post_install_cloudflare_sha256": bundle.post_install_cloudflare_sha256,
        "preexisting_cloudflare_sha256": bundle.preexisting_cloudflare_sha256,
        "proof_commit": bundle.proof_commit,
        "schema_version": 2,
        "snapshot_evidence": (
            None if bundle.snapshot_evidence is None else bundle.snapshot_evidence.to_payload()
        ),
        "source_base_commit": bundle.source_base_commit,
        "uploaded_env_sha256": bundle.uploaded_env_sha256,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def parse_bundle(content: bytes) -> Task1FinalizationBundle:
    """Parse only the canonical, bounded v2 recovery schema."""
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AbortGuardError("Task 1 finalization bundle is invalid") from error
    expected = {
        "coder_secret_inventory_sha256",
        "context_sha256",
        "guard_id",
        "host_identity_mode",
        "host_sha256",
        "images",
        "legacy_workspace_managed_fingerprints_sha256",
        "payload_sha256",
        "payloads",
        "phase",
        "post_install_cloudflare_sha256",
        "preexisting_cloudflare_sha256",
        "proof_commit",
        "schema_version",
        "snapshot_evidence",
        "source_base_commit",
        "uploaded_env_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 2:
        raise AbortGuardError("Task 1 finalization bundle is invalid")
    try:
        bundle = _bundle_from_value(value)
        payload_hashes = _string_mapping(value["payload_sha256"])
    except (TypeError, ValueError) as error:
        raise AbortGuardError("Task 1 finalization bundle is invalid") from error
    if payload_hashes != {
        name: artifacts.sha256_bytes(payload) for name, payload in bundle.payloads.items()
    }:
        raise AbortGuardError("Task 1 finalization bundle payload hashes are invalid")
    if content != serialize_bundle(bundle):
        raise AbortGuardError("Task 1 finalization bundle is not canonical")
    return bundle


def _bundle_from_value(value: dict[str, JsonValue]) -> Task1FinalizationBundle:
    snapshot_payload = value["snapshot_evidence"]
    snapshot_evidence = (
        None
        if snapshot_payload is None
        else Task1CloudflareSnapshotEvidenceV1.from_payload(
            require_mapping(snapshot_payload, "Task 1 snapshot evidence")
        )
    )
    return Task1FinalizationBundle(
        guard_id=_text(value["guard_id"]),
        context_sha256=_text(value["context_sha256"]),
        uploaded_env_sha256=_text(value["uploaded_env_sha256"]),
        host_sha256=_text(value["host_sha256"]),
        source_base_commit=_text(value["source_base_commit"]),
        proof_commit=_text(value["proof_commit"]),
        host_identity_mode=_mode(value["host_identity_mode"]),
        phase=FinalizationBundlePhase(_text(value["phase"])),
        payloads=_decode_payloads(value["payloads"]),
        images=_string_mapping(value["images"]),
        coder_secret_inventory_sha256=_text(value["coder_secret_inventory_sha256"]),
        legacy_workspace_managed_fingerprints_sha256=_text(
            value["legacy_workspace_managed_fingerprints_sha256"]
        ),
        preexisting_cloudflare_sha256=_text(value["preexisting_cloudflare_sha256"]),
        post_install_cloudflare_sha256=_optional_text(value["post_install_cloudflare_sha256"]),
        snapshot_evidence=snapshot_evidence,
    )


def _decode_payloads(value: JsonValue) -> dict[str, bytes]:
    encoded = _string_mapping(value)
    try:
        return {name: base64.b64decode(content, validate=True) for name, content in encoded.items()}
    except ValueError as error:
        raise AbortGuardError("Task 1 finalization bundle payloads are invalid") from error


def _validate_bundle(bundle: Task1FinalizationBundle) -> None:
    hashes = (
        bundle.guard_id,
        bundle.context_sha256,
        bundle.uploaded_env_sha256,
        bundle.host_sha256,
        bundle.coder_secret_inventory_sha256,
        bundle.legacy_workspace_managed_fingerprints_sha256,
        bundle.preexisting_cloudflare_sha256,
    )
    if not all(_HASH.fullmatch(value) for value in hashes):
        raise AbortGuardError("Task 1 finalization bundle hashes are invalid")
    if not (
        _COMMIT.fullmatch(bundle.source_base_commit) and _COMMIT.fullmatch(bundle.proof_commit)
    ):
        raise AbortGuardError("Task 1 finalization bundle commits are invalid")
    image_names = frozenset(bundle.images)
    if not _REQUIRED_IMAGE_NAMES <= image_names <= _IMAGE_NAMES or any(
        not value for value in bundle.images.values()
    ):
        raise AbortGuardError("Task 1 finalization bundle images are invalid")
    _validate_payloads(bundle.payloads, bundle.host_identity_mode)
    match bundle.phase:
        case FinalizationBundlePhase.PENDING_CLEANUP:
            if (
                bundle.post_install_cloudflare_sha256 is not None
                or bundle.snapshot_evidence is not None
            ):
                raise AbortGuardError("Task 1 pending finalization bundle has cleanup evidence")
        case FinalizationBundlePhase.READY:
            if (
                bundle.post_install_cloudflare_sha256 is None
                or not _HASH.fullmatch(bundle.post_install_cloudflare_sha256)
                or bundle.snapshot_evidence is None
                or bundle.snapshot_evidence.context_sha256 != bundle.context_sha256
            ):
                raise AbortGuardError("Task 1 finalization bundle lacks valid cleanup evidence")


def _validate_payloads(payloads: dict[str, bytes], mode: proof.HostIdentityMode) -> None:
    expected = (
        {"baseline.json", "host-a-preflight.json", "host-b-preflight.json"}
        if mode == "distinct"
        else {"baseline.json", "host-a-preflight.json", "single-host-lifecycle-baseline.json"}
    )
    if set(payloads) != expected or not set(payloads) <= _PAYLOAD_NAMES:
        raise AbortGuardError("Task 1 finalization bundle payload names are invalid")
    if any(
        not content.endswith(b"\n") or len(content) > 16 * 1024 * 1024
        for content in payloads.values()
    ):
        raise AbortGuardError("Task 1 finalization bundle payload bytes are invalid")


def _string_mapping(value: JsonValue) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise AbortGuardError("Task 1 finalization bundle mapping is invalid")
    return dict(value)


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise AbortGuardError("Task 1 finalization bundle text field is invalid")
    return value


def _optional_text(value: JsonValue) -> str | None:
    return None if value is None else _text(value)


def _mode(value: JsonValue) -> proof.HostIdentityMode:
    match value:
        case "distinct":
            return "distinct"
        case "single_sequential":
            return "single_sequential"
        case _:
            raise AbortGuardError("Task 1 finalization bundle mode is invalid")
