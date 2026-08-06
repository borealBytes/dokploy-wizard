from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from dokploy_wizard.proof.model_sync_state_upgrade_observation import (
    observe_state_upgrade_authority,
)
from dokploy_wizard.state import AppliedStateCheckpoint, parse_env_file, resolve_desired_state
from dokploy_wizard.state.upgrade import state_upgrade_paths

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"


def test_observation_rejects_malformed_raw_input_without_unbound_local(tmp_path: Path) -> None:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    paths = state_upgrade_paths(tmp_path)
    paths["desired"].write_text(json.dumps(desired.to_dict()), encoding="utf-8")
    paths["applied"].write_text(
        json.dumps(
            AppliedStateCheckpoint(
                format_version=desired.format_version,
                desired_state_fingerprint="f" * 64,
                completed_steps=(),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    paths["raw_input"].write_text(
        json.dumps({"format_version": 1, "values": []}),
        encoding="utf-8",
    )

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["raw_input_reconstruction_failure"] == "state_validation"
    assert observation["raw_input_desired_reconstructable"] is False
    assert observation["receipt_raw_desired_reconstructable"] is False
    assert observation["applied_matches_receipt_raw_desired"] is False
    assert observation["applied_matches_legacy_receipt_without_runtime_images"] is False


def test_observation_matches_combined_legacy_receipt_candidate(tmp_path: Path) -> None:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    desired = replace(
        desired,
        hostnames={
            **desired.hostnames,
            "litellm-admin": f"litellm-admin.{desired.root_domain}",
        },
    )
    legacy_token = "legacy-openclaw-token"
    receipt_raw = replace(
        raw,
        values={
            **raw.values,
            "DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD": "true",
            "DOKPLOY_WIZARD_TASK1_PROOF_CONTEXT_ID": "a" * 64,
            "LITELLM_ADMIN_SUBDOMAIN": "litellm-admin",
            "OPENCLAW_GATEWAY_TOKEN": legacy_token,
        },
    )
    legacy_desired = desired.to_dict()
    legacy_desired.pop("runtime_images")
    legacy_desired["openclaw_gateway_token"] = legacy_token
    legacy_fingerprint = sha256(
        json.dumps(legacy_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    paths = state_upgrade_paths(tmp_path)
    paths["raw_input"].write_text(json.dumps(receipt_raw.to_dict()), encoding="utf-8")
    paths["desired"].write_text(json.dumps(desired.to_dict()), encoding="utf-8")
    paths["applied"].write_text(
        json.dumps(
            AppliedStateCheckpoint(
                format_version=desired.format_version,
                desired_state_fingerprint=legacy_fingerprint,
                completed_steps=(),
            ).to_dict()
        ),
        encoding="utf-8",
    )

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["applied_matches_current_desired"] is False
    assert observation["applied_matches_parsed_current_desired"] is False
    assert observation["applied_matches_current_without_runtime_images"] is False
    assert observation["applied_matches_receipt_raw_desired"] is False
    assert observation["applied_matches_legacy_receipt_without_runtime_images"] is True
