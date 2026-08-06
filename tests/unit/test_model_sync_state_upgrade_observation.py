from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_state_upgrade_observation import (
    observe_state_upgrade_authority,
)
from dokploy_wizard.state import AppliedStateCheckpoint, parse_env_file, resolve_desired_state
from dokploy_wizard.state.upgrade import state_upgrade_paths
from dokploy_wizard.state.upgrade_intent import ALL_KEYS, StateUpgradeIntent
from dokploy_wizard.state.upgrade_io import file_hash

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


def _write_authorized_interruption(state_dir: Path) -> None:
    paths = state_upgrade_paths(state_dir)
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw).to_dict()
    paths["raw_input"].write_text(json.dumps(raw.to_dict()), encoding="utf-8")
    desired_bytes = json.dumps(desired, sort_keys=True, separators=(",", ":")).encode()
    (paths["desired"]).write_bytes(desired_bytes)
    (paths["applied"]).write_text(
        json.dumps(
            AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint="f" * 64,
                completed_steps=(),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    owner = state_dir / "shared-core-sync-owner.json"
    owner.write_text(json.dumps({"owner_id": _OWNER, "schema_version": 1}), encoding="utf-8")
    post = {key: "MISSING" for key in ALL_KEYS}
    post["raw_input"] = file_hash(paths["raw_input"])
    post["owner"] = file_hash(owner)
    post["desired"] = file_hash(paths["desired"])
    post["applied"] = file_hash(paths["applied"])
    pre = dict(post)
    pre["desired"] = sha256(b"preimage").hexdigest()
    intent = StateUpgradeIntent(
        generation=1,
        cas_token="a" * 64,
        status="writing",
        owner_id=_OWNER,
        pre_hashes=pre,
        post_hashes=post,
        completed_writes=("owner", "desired"),
        created_at="2026-08-05T00:00:00+00:00",
        updated_at="2026-08-05T00:00:00+00:00",
    )
    (state_dir / "state-upgrade-intent-v1.json").write_text(
        json.dumps(intent.to_dict()), encoding="utf-8"
    )


def test_observation_authorizes_missing_optional_documents_when_intent_binds_missing(
    tmp_path: Path,
) -> None:
    _write_authorized_interruption(tmp_path)

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["desired_present"] is True
    assert observation["applied_present"] is True
    assert observation["intent_hash_map_matches_current_state"] is True
    assert observation["intent_authorizes_desired_applied_mixed_state"] is True
    assert observation["applied_matches_raw_input_desired"] is False


def test_observation_reports_raw_input_preimage_fingerprint_match(tmp_path: Path) -> None:
    _write_authorized_interruption(tmp_path)
    paths = state_upgrade_paths(tmp_path)
    desired = resolve_desired_state(parse_env_file(_FIXTURE))
    paths["applied"].write_text(
        json.dumps(
            AppliedStateCheckpoint(
                format_version=desired.format_version,
                desired_state_fingerprint=desired.fingerprint(),
                completed_steps=(),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = StateUpgradeIntent.from_dict(json.loads(intent_path.read_text(encoding="utf-8")))
    applied_hash = file_hash(paths["applied"])
    updated_intent = replace(
        intent,
        pre_hashes={**intent.pre_hashes, "applied": applied_hash},
        post_hashes={**intent.post_hashes, "applied": applied_hash},
    )
    intent_path.write_text(json.dumps(updated_intent.to_dict()), encoding="utf-8")

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["applied_matches_raw_input_desired"] is True
    assert "desired_state_fingerprint" not in observation


def test_observation_reports_unreconstructable_raw_input_without_values(tmp_path: Path) -> None:
    _write_authorized_interruption(tmp_path)
    paths = state_upgrade_paths(tmp_path)
    paths["raw_input"].write_text(
        json.dumps({"format_version": 1, "values": {"ROOT_DOMAIN": ""}}),
        encoding="utf-8",
    )
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = StateUpgradeIntent.from_dict(json.loads(intent_path.read_text(encoding="utf-8")))
    raw_input_hash = file_hash(paths["raw_input"])
    updated_intent = replace(
        intent,
        pre_hashes={**intent.pre_hashes, "raw_input": raw_input_hash},
        post_hashes={**intent.post_hashes, "raw_input": raw_input_hash},
    )
    intent_path.write_text(json.dumps(updated_intent.to_dict()), encoding="utf-8")

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["raw_input_desired_reconstructable"] is False
    assert observation["applied_matches_raw_input_desired"] is False


def test_observation_reports_context_bound_raw_input_as_unreconstructable(tmp_path: Path) -> None:
    _write_authorized_interruption(tmp_path)
    paths = state_upgrade_paths(tmp_path)
    raw = parse_env_file(_FIXTURE)
    context_bound_raw = replace(
        raw,
        values={**raw.values, "DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD": "true"},
    )
    paths["raw_input"].write_text(json.dumps(context_bound_raw.to_dict()), encoding="utf-8")
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = StateUpgradeIntent.from_dict(json.loads(intent_path.read_text(encoding="utf-8")))
    raw_input_hash = file_hash(paths["raw_input"])
    updated_intent = replace(
        intent,
        pre_hashes={**intent.pre_hashes, "raw_input": raw_input_hash},
        post_hashes={**intent.post_hashes, "raw_input": raw_input_hash},
    )
    intent_path.write_text(json.dumps(updated_intent.to_dict()), encoding="utf-8")

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["raw_input_desired_reconstructable"] is False
    assert observation["raw_input_reconstruction_failure"] == "task1_context"
    assert observation["receipt_raw_desired_reconstructable"] is True


def test_observation_compares_applied_to_receipt_raw_without_proof_controls(
    tmp_path: Path,
) -> None:
    _write_authorized_interruption(tmp_path)
    paths = state_upgrade_paths(tmp_path)
    raw = parse_env_file(_FIXTURE)
    context_bound_raw = replace(
        raw,
        values={
            **raw.values,
            "DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD": "true",
            "DOKPLOY_WIZARD_TASK1_PROOF_CONTEXT_ID": "a" * 64,
        },
    )
    paths["raw_input"].write_text(json.dumps(context_bound_raw.to_dict()), encoding="utf-8")
    desired = resolve_desired_state(raw)
    applied = AppliedStateCheckpoint.from_dict(
        json.loads(paths["applied"].read_text(encoding="utf-8"))
    )
    paths["applied"].write_text(
        json.dumps(replace(applied, desired_state_fingerprint=desired.fingerprint()).to_dict()),
        encoding="utf-8",
    )
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = StateUpgradeIntent.from_dict(json.loads(intent_path.read_text(encoding="utf-8")))
    raw_input_hash = file_hash(paths["raw_input"])
    applied_hash = file_hash(paths["applied"])
    intent_path.write_text(
        json.dumps(
            replace(
                intent,
                pre_hashes={
                    **intent.pre_hashes,
                    "applied": applied_hash,
                    "raw_input": raw_input_hash,
                },
                post_hashes={
                    **intent.post_hashes,
                    "applied": applied_hash,
                    "raw_input": raw_input_hash,
                },
            ).to_dict()
        ),
        encoding="utf-8",
    )

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["raw_input_reconstruction_failure"] == "task1_context"
    assert observation["receipt_raw_desired_reconstructable"] is True
    assert observation["applied_matches_receipt_raw_desired"] is True


def test_observation_compares_applied_to_legacy_runtime_image_shape(tmp_path: Path) -> None:
    _write_authorized_interruption(tmp_path)
    paths = state_upgrade_paths(tmp_path)
    desired_payload = json.loads(paths["desired"].read_text(encoding="utf-8"))
    desired_payload.pop("runtime_images")
    legacy_fingerprint = sha256(
        json.dumps(desired_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    applied = AppliedStateCheckpoint.from_dict(
        json.loads(paths["applied"].read_text(encoding="utf-8"))
    )
    paths["applied"].write_text(
        json.dumps(replace(applied, desired_state_fingerprint=legacy_fingerprint).to_dict()),
        encoding="utf-8",
    )
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = StateUpgradeIntent.from_dict(json.loads(intent_path.read_text(encoding="utf-8")))
    applied_hash = file_hash(paths["applied"])
    intent_path.write_text(
        json.dumps(
            replace(
                intent,
                pre_hashes={**intent.pre_hashes, "applied": applied_hash},
                post_hashes={**intent.post_hashes, "applied": applied_hash},
            ).to_dict()
        ),
        encoding="utf-8",
    )

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["applied_matches_current_desired"] is False
    assert observation["applied_matches_parsed_current_desired"] is False
    assert observation["applied_matches_current_without_runtime_images"] is True


@pytest.mark.parametrize(
    ("key", "expected"),
    (("desired", (False, True)), ("applied", (True, False))),
)
def test_observation_rejects_missing_desired_or_applied_documents(
    tmp_path: Path, key: str, expected: tuple[bool, bool]
) -> None:
    _write_authorized_interruption(tmp_path)
    paths = state_upgrade_paths(tmp_path)
    paths[key].unlink()

    observation = observe_state_upgrade_authority(tmp_path)

    assert (observation["desired_present"], observation["applied_present"]) == expected
    assert "intent_authorizes_desired_applied_mixed_state" not in observation


@pytest.mark.parametrize("hashes", ("desired", "applied"))
def test_observation_rejects_invalid_recovery_image_shape(tmp_path: Path, hashes: str) -> None:
    _write_authorized_interruption(tmp_path)
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["pre_hashes"][hashes] = intent["post_hashes"][hashes]
    if hashes == "applied":
        intent["pre_hashes"][hashes] = "a" * 64
    intent_path.write_text(json.dumps(intent), encoding="utf-8")

    observation = observe_state_upgrade_authority(tmp_path)

    assert observation["intent_authorizes_desired_applied_mixed_state"] is False
