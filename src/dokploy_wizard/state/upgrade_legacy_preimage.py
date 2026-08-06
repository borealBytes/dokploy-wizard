"""Semantic reconstruction of legacy runtime-upgrade desired state."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

from dokploy_wizard.proof.model_sync_task1_context import Task1ProofContextError
from dokploy_wizard.proof.model_sync_task1_context_schema import PROOF_CONTROL_KEYS
from dokploy_wizard.state.env import resolve_desired_state
from dokploy_wizard.state.models import (
    AppliedStateCheckpoint,
    DesiredState,
    RawEnvInput,
    StateValidationError,
)


def legacy_receipt_without_runtime_images_fingerprint(
    raw_input: RawEnvInput,
    desired: DesiredState,
    applied: AppliedStateCheckpoint,
) -> str | None:
    """Return the exact Task 1 semantic preimage when it is reconstructable."""

    if applied.runtime_images is not None:
        return None
    has_proof_controls = bool(set(raw_input.values) & PROOF_CONTROL_KEYS)
    receipt_values = {
        key: value for key, value in raw_input.values.items() if key not in PROOF_CONTROL_KEYS
    }
    legacy_token = receipt_values.get("OPENCLAW_GATEWAY_TOKEN")
    restores_disabled_token = (
        legacy_token is not None
        and legacy_token != ""
        and desired.openclaw_gateway_token is None
        and "openclaw" not in desired.enabled_packs
    )
    projection_values = dict(receipt_values)
    if restores_disabled_token:
        projection_values.pop("OPENCLAW_GATEWAY_TOKEN")
    receipt_raw = RawEnvInput(
        format_version=raw_input.format_version,
        values=projection_values,
    )
    try:
        legacy_desired = resolve_desired_state(receipt_raw)
    except (StateValidationError, Task1ProofContextError):
        return None
    if has_proof_controls:
        if not PROOF_CONTROL_KEYS.issubset(raw_input.values):
            return None
        if raw_input.values["DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD"] != "true":
            return None
        litellm_admin_subdomain = receipt_values.get("LITELLM_ADMIN_SUBDOMAIN")
        if litellm_admin_subdomain is None or litellm_admin_subdomain == "":
            return None
        projected_hostnames = dict(legacy_desired.hostnames)
        projected_hostnames.pop("coder-wildcard", None)
        projected_hostnames["litellm-admin"] = (
            f"{litellm_admin_subdomain}.{legacy_desired.root_domain}"
        )
        legacy_desired = replace(
            legacy_desired,
            hostnames=dict(sorted(projected_hostnames.items())),
        )
    legacy_payload = legacy_desired.to_dict()
    legacy_payload.pop("runtime_images", None)
    if restores_disabled_token:
        legacy_payload["openclaw_gateway_token"] = legacy_token
    current_legacy_payload = desired.to_dict()
    current_legacy_payload.pop("runtime_images", None)
    current_shared_core = current_legacy_payload.get("shared_core")
    if applied.opencode_go_sync is None and isinstance(current_shared_core, dict):
        current_shared_core = dict(current_shared_core)
        current_shared_core.pop("opencode_go_sync", None)
        current_legacy_payload["shared_core"] = current_shared_core
    if restores_disabled_token:
        current_legacy_payload["openclaw_gateway_token"] = legacy_token
    if legacy_payload != current_legacy_payload:
        return None
    return sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
