"""Exact terminal-receipt recovery for runtime state projections."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING, Callable

from dokploy_wizard.proof.model_sync_task1_context import Task1ProofContextError
from dokploy_wizard.state.env import resolve_desired_state
from dokploy_wizard.state.models import (
    AppliedStateCheckpoint,
    RawEnvInput,
    StateValidationError,
)
from dokploy_wizard.state.runtime_images import RuntimeImageError, resolve_runtime_images
from dokploy_wizard.state.store import parse_desired_state_payload
from dokploy_wizard.state.sync_schema import JsonValue
from dokploy_wizard.state.upgrade_intent import (
    ALL_KEYS,
    WRITE_ORDER,
    StateUpgradeError,
    StateUpgradeIntent,
)
from dokploy_wizard.state.upgrade_io import file_hash, read_json
from dokploy_wizard.state.upgrade_legacy_preimage import (
    legacy_receipt_without_runtime_images_fingerprint,
)

if TYPE_CHECKING:
    from dokploy_wizard.state.upgrade_projection import RuntimeProjectionRequest


def recover_terminal_runtime_projection(
    request: RuntimeProjectionRequest,
    intent: StateUpgradeIntent,
    build_runtime_target: Callable[[RuntimeProjectionRequest], dict[str, dict[str, JsonValue]]],
) -> dict[str, dict[str, JsonValue]]:
    """Reopen only an exact terminal receipt with a stale applied checkpoint."""

    if intent.owner_id != request.owner_id:
        raise StateUpgradeError("State upgrade complete intent owner does not match the request.")
    if set(request.paths) != set(ALL_KEYS):
        raise StateUpgradeError("State upgrade complete intent paths are incomplete.")
    if intent.completed_writes != WRITE_ORDER:
        raise StateUpgradeError("State upgrade complete intent writes are incomplete.")
    if {key: file_hash(request.paths[key]) for key in ALL_KEYS} != intent.post_hashes:
        raise StateUpgradeError("State upgrade complete intent bytes do not match the receipt.")
    try:
        applied = AppliedStateCheckpoint.from_dict(read_json(request.paths["applied"]))
    except StateValidationError as error:
        raise StateUpgradeError(
            "State upgrade complete intent applied state is invalid."
        ) from error
    try:
        raw_input = RawEnvInput.from_dict(read_json(request.paths["raw_input"]))
    except (OSError, json.JSONDecodeError, StateValidationError) as error:
        raise StateUpgradeError("State upgrade complete intent raw input is invalid.") from error
    desired_payload = read_json(request.paths["desired"])
    try:
        desired = parse_desired_state_payload(desired_payload)
    except StateValidationError as error:
        raise StateUpgradeError(
            "State upgrade complete intent desired state is invalid."
        ) from error
    try:
        raw_fingerprint = resolve_desired_state(raw_input).fingerprint()
    except (StateValidationError, Task1ProofContextError):
        preimage_fingerprints: set[str] = set()
    else:
        preimage_fingerprints = {raw_fingerprint}
    legacy_receipt_fingerprint = legacy_receipt_without_runtime_images_fingerprint(
        raw_input,
        desired,
        applied,
    )
    if legacy_receipt_fingerprint is not None:
        preimage_fingerprints.add(legacy_receipt_fingerprint)
    if applied.runtime_images is None:
        legacy_payload = dict(desired_payload)
        legacy_token = raw_input.values.get("OPENCLAW_GATEWAY_TOKEN")
        if (
            legacy_token
            and desired.openclaw_gateway_token is None
            and "openclaw" not in desired.enabled_packs
        ):
            legacy_payload["openclaw_gateway_token"] = legacy_token
        try:
            raw_runtime_images = resolve_runtime_images(raw_input.values)
        except RuntimeImageError:
            raw_runtime_images = None
        if raw_runtime_images is not None:
            legacy_payload["runtime_images"] = raw_runtime_images.to_dict()
            preimage_fingerprints.add(
                sha256(
                    json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
    protected_keys = ("raw_input", "litellm_keys", "surfsense_secrets", "seaweedfs_secrets")
    if any(intent.pre_hashes[key] != intent.post_hashes[key] for key in protected_keys):
        raise StateUpgradeError("State upgrade complete intent changed protected bytes.")
    terminal_receipt_authorized = (
        intent.pre_hashes["desired"] != intent.post_hashes["desired"]
        and intent.pre_hashes["applied"] == intent.post_hashes["applied"]
        and applied.desired_state_fingerprint in preimage_fingerprints
    )
    legacy_noop_authorized = (
        all(intent.pre_hashes[key] == intent.post_hashes[key] for key in ALL_KEYS)
        and legacy_receipt_fingerprint is not None
        and applied.desired_state_fingerprint == legacy_receipt_fingerprint
    )
    if not terminal_receipt_authorized and not legacy_noop_authorized:
        raise StateUpgradeError(
            "State upgrade complete intent does not bind the stale applied image."
        )
    return build_runtime_target(
        replace(request, expected_applied_fingerprint=applied.desired_state_fingerprint)
    )
