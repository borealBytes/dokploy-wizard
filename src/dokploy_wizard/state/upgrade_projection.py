"""Canonical desired/applied/ledger projection for outer-v1 state upgrades."""

from __future__ import annotations

import json
from dataclasses import replace
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
from dokploy_wizard.state.upgrade_intent import StateUpgradeError
from dokploy_wizard.state.upgrade_io import canonical_bytes, file_hash, read_json


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
        raise StateUpgradeError("State upgrade applied fingerprint mismatch.")
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
    paths: Mapping[str, Path],
    runtime_images: RuntimeImages,
    ownership_ledger: OwnershipLedger,
) -> dict[str, dict[str, JsonValue]]:
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
    if applied.desired_state_fingerprint != legacy_fingerprint:
        raise StateUpgradeError("State upgrade applied fingerprint mismatch.")
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


def targets_match(
    target: Mapping[str, Mapping[str, JsonValue]],
    paths: Mapping[str, Path],
) -> bool:
    return all(
        file_hash(paths[key]) == sha256(canonical_bytes(payload)).hexdigest()
        for key, payload in target.items()
    )
