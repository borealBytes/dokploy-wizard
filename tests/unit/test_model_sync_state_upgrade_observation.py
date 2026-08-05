from __future__ import annotations

import json
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
    desired = resolve_desired_state(parse_env_file(_FIXTURE)).to_dict()
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
