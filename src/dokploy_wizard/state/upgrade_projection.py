"""Canonical desired/applied/ledger projection for outer-v1 state upgrades."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from dokploy_wizard.state.models import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    StateValidationError,
)
from dokploy_wizard.state.runtime_images import RuntimeImages
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    SyncDesiredState,
    SyncStateError,
)
from dokploy_wizard.state.store import parse_desired_state_payload
from dokploy_wizard.state.sync_schema import JsonValue
from dokploy_wizard.state.upgrade_intent import (
    ALL_KEYS,
    StateUpgradeError,
    StateUpgradeIntent,
)
from dokploy_wizard.state.upgrade_io import canonical_bytes, file_hash, read_json
from dokploy_wizard.state.upgrade_terminal_recovery import recover_terminal_runtime_projection


class StateUpgradeAppliedFingerprintMismatch(StateUpgradeError):
    """Raised when an applied checkpoint does not bind the expected desired state."""


@dataclass(frozen=True, slots=True)
class RuntimeProjectionRequest:
    """Runtime-image target projection and its eligible resume context."""

    state_dir: Path
    owner_id: str
    paths: Mapping[str, Path]
    runtime_images: RuntimeImages
    ownership_ledger: OwnershipLedger
    expected_applied_fingerprint: str | None = None


def target_documents(
    paths: Mapping[str, Path],
    sync_desired: SyncDesiredState,
    sync_applied: AppliedSyncState,
    ownership_ledger: OwnershipLedger | None,
) -> dict[str, dict[str, JsonValue]]:
    try:
        desired_payload = json.loads(paths["desired"].read_text(encoding="utf-8"))
        applied_payload = json.loads(paths["applied"].read_text(encoding="utf-8"))
        ledger_payload = json.loads(paths["ledger"].read_text(encoding="utf-8"))
        desired = parse_desired_state_payload(desired_payload)
        applied = AppliedStateCheckpoint.from_dict(applied_payload)
        ledger = OwnershipLedger.from_dict(ledger_payload)
    except (OSError, json.JSONDecodeError, StateValidationError, SyncStateError) as error:
        raise StateUpgradeError("State upgrade input document is invalid.") from error
    legacy_fingerprint = sha256(
        json.dumps(desired_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    upgraded_desired = replace(desired, opencode_go_sync=sync_desired)
    if applied.desired_state_fingerprint != legacy_fingerprint:
        raise StateUpgradeAppliedFingerprintMismatch("State upgrade applied fingerprint mismatch.")
    if sync_applied.desired_fingerprint != sync_desired.fingerprint():
        raise StateUpgradeError("State upgrade nested applied fingerprint mismatch.")
    previous_legacy = (
        legacy_fingerprint
        if applied.opencode_go_sync is None
        else applied.opencode_go_sync.previous_legacy_fingerprint
    )
    nested_applied = replace(
        sync_applied,
        previous_legacy_fingerprint=previous_legacy,
    )
    upgraded_applied = replace(
        applied,
        desired_state_fingerprint=upgraded_desired.fingerprint(),
        runtime_images=upgraded_desired.runtime_images,
        opencode_go_sync=nested_applied,
    )
    return {
        "desired": upgraded_desired.to_dict(),
        "applied": upgraded_applied.to_dict(),
        "ledger": (ownership_ledger or ledger).to_dict(),
    }


def ledger_target_documents(
    paths: Mapping[str, Path],
    ownership_ledger: OwnershipLedger,
) -> dict[str, dict[str, JsonValue]]:
    desired = read_json(paths["desired"])
    applied = read_json(paths["applied"])
    if not isinstance(desired, dict) or not isinstance(applied, dict):
        raise StateUpgradeError("State upgrade input document is invalid.")
    return {
        "desired": desired,
        "applied": applied,
        "ledger": ownership_ledger.to_dict(),
    }


def runtime_target_documents(
    request: RuntimeProjectionRequest,
) -> dict[str, dict[str, JsonValue]]:
    paths = request.paths
    runtime_images = request.runtime_images
    ownership_ledger = request.ownership_ledger
    expected_applied_fingerprint = request.expected_applied_fingerprint
    try:
        desired_payload = json.loads(paths["desired"].read_text(encoding="utf-8"))
        applied_payload = json.loads(paths["applied"].read_text(encoding="utf-8"))
        desired = parse_desired_state_payload(desired_payload)
        applied = AppliedStateCheckpoint.from_dict(applied_payload)
    except (OSError, json.JSONDecodeError, StateValidationError, SyncStateError) as error:
        raise StateUpgradeError("State upgrade input document is invalid.") from error
    legacy_fingerprint = sha256(
        json.dumps(desired_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    expected_fingerprint = expected_applied_fingerprint or legacy_fingerprint
    if applied.desired_state_fingerprint != expected_fingerprint:
        raise StateUpgradeAppliedFingerprintMismatch("State upgrade applied fingerprint mismatch.")
    upgraded_desired = replace(desired, runtime_images=runtime_images)
    upgraded_applied = replace(
        applied,
        desired_state_fingerprint=upgraded_desired.fingerprint(),
        runtime_images=runtime_images,
    )
    return {
        "desired": upgraded_desired.to_dict(),
        "applied": upgraded_applied.to_dict(),
        "ledger": ownership_ledger.to_dict(),
    }


def runtime_target_documents_or_resume(
    request: RuntimeProjectionRequest,
) -> dict[str, dict[str, JsonValue]]:
    """Build the normal runtime target or resume one exact desired-write interruption."""

    try:
        return runtime_target_documents(request)
    except StateUpgradeAppliedFingerprintMismatch:
        intent_path = request.state_dir / "state-upgrade-intent-v1.json"
        if not intent_path.exists():
            raise
        intent = StateUpgradeIntent.from_dict(read_json(intent_path))
        if intent.status == "complete":
            return runtime_complete_intent_target_documents(request, intent)
        return runtime_resume_target_documents(request, intent)


def runtime_complete_intent_target_documents(
    request: RuntimeProjectionRequest, intent: StateUpgradeIntent
) -> dict[str, dict[str, JsonValue]]:
    """Reopen an exact terminal receipt whose applied checkpoint is semantically stale."""

    return recover_terminal_runtime_projection(request, intent, runtime_target_documents)


def runtime_resume_target_documents(
    request: RuntimeProjectionRequest,
    intent: StateUpgradeIntent,
) -> dict[str, dict[str, JsonValue]]:
    """Rebuild a runtime projection only from its exact desired-write interruption."""

    if intent.owner_id != request.owner_id:
        raise StateUpgradeError("State upgrade recovery intent owner does not match the request.")
    if intent.status != "writing" or intent.completed_writes != ("owner", "desired"):
        raise StateUpgradeError("State upgrade recovery intent is not at the desired-write stage.")
    if set(request.paths) != set(ALL_KEYS):
        raise StateUpgradeError("State upgrade recovery paths are incomplete.")
    if any(
        value != "MISSING"
        and (len(value) != 64 or set(value) - set("0123456789abcdef"))
        for hashes in (intent.pre_hashes, intent.post_hashes)
        for value in hashes.values()
    ) or any(
        intent_hash == "MISSING"
        for intent_hash in (
            intent.pre_hashes["desired"],
            intent.post_hashes["desired"],
            intent.pre_hashes["applied"],
            intent.post_hashes["applied"],
        )
    ):
        raise StateUpgradeError("State upgrade recovery intent hashes are invalid.")
    expected_hashes = {
        key: intent.post_hashes[key] if key in intent.completed_writes else intent.pre_hashes[key]
        for key in ALL_KEYS
    }
    if any(file_hash(request.paths[key]) != expected_hashes[key] for key in ALL_KEYS):
        raise StateUpgradeError("State upgrade recovery bytes do not match the intent.")
    if (
        intent.pre_hashes["desired"] == intent.post_hashes["desired"]
        or intent.pre_hashes["applied"] != intent.post_hashes["applied"]
    ):
        raise StateUpgradeError("State upgrade recovery intent does not bind the expected images.")
    try:
        preimage_applied = AppliedStateCheckpoint.from_dict(read_json(request.paths["applied"]))
    except StateValidationError as error:
        raise StateUpgradeError("State upgrade recovery applied state is invalid.") from error
    target = runtime_target_documents(
        replace(
            request,
            expected_applied_fingerprint=preimage_applied.desired_state_fingerprint,
        )
    )
    desired_hash = sha256(canonical_bytes(target["desired"])).hexdigest()
    target_fingerprint = sha256(
        json.dumps(target["desired"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        desired_hash != intent.post_hashes["desired"]
        or file_hash(request.paths["desired"]) != desired_hash
        or target["applied"].get("desired_state_fingerprint") != target_fingerprint
    ):
        raise StateUpgradeError("State upgrade recovery target does not match the intent.")
    return target


def targets_match(
    target: Mapping[str, Mapping[str, JsonValue]],
    paths: Mapping[str, Path],
) -> bool:
    return all(
        file_hash(paths[key]) == sha256(canonical_bytes(payload)).hexdigest()
        for key, payload in target.items()
    )
