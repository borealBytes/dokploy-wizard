"""Exact terminal-receipt recovery for runtime state projections."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from dokploy_wizard.proof.model_sync_task1_context import Task1ProofContextError
from dokploy_wizard.state.env import resolve_desired_state
from dokploy_wizard.state.models import (
    AppliedStateCheckpoint,
    RawEnvInput,
    StateValidationError,
)
from dokploy_wizard.state.sync_schema import JsonValue
from dokploy_wizard.state.upgrade_intent import (
    ALL_KEYS,
    WRITE_ORDER,
    StateUpgradeError,
    StateUpgradeIntent,
)
from dokploy_wizard.state.upgrade_io import file_hash, read_json

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
    try:
        preimage_fingerprint = resolve_desired_state(raw_input).fingerprint()
    except (StateValidationError, Task1ProofContextError):
        preimage_fingerprint = None
    protected_keys = ("raw_input", "litellm_keys", "surfsense_secrets", "seaweedfs_secrets")
    if any(intent.pre_hashes[key] != intent.post_hashes[key] for key in protected_keys):
        raise StateUpgradeError("State upgrade complete intent changed protected bytes.")
    if (
        intent.pre_hashes["desired"] == intent.post_hashes["desired"]
        or intent.pre_hashes["applied"] != intent.post_hashes["applied"]
        or len(applied.desired_state_fingerprint) != 64
        or bool(set(applied.desired_state_fingerprint) - set("0123456789abcdef"))
        or (
            preimage_fingerprint is not None
            and applied.desired_state_fingerprint != preimage_fingerprint
            and applied.desired_state_fingerprint != intent.pre_hashes["desired"]
        )
    ):
        raise StateUpgradeError(
            "State upgrade complete intent does not bind the stale applied image."
        )
    return build_runtime_target(
        replace(request, expected_applied_fingerprint=applied.desired_state_fingerprint)
    )
