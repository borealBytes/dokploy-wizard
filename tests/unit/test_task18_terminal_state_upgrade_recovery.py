from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Mapping

import pytest

from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
)
from dokploy_wizard.state.runtime_images import RuntimeImages
from dokploy_wizard.state.shared_core_sync import SyncOwner
from dokploy_wizard.state.store import parse_desired_state_payload
from dokploy_wizard.state.upgrade import state_upgrade_paths, upgrade_state_contract
from dokploy_wizard.state.upgrade_intent import (
    ALL_KEYS,
    WRITE_ORDER,
    StateUpgradeError,
    StateUpgradeIntent,
)
from dokploy_wizard.state.upgrade_io import atomic_json, file_hash, read_json
from dokploy_wizard.state.upgrade_projection import (
    RuntimeProjectionRequest,
    runtime_complete_intent_target_documents,
)

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"
_INTENT = "state-upgrade-intent-v1.json"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


@dataclass(frozen=True, slots=True)
class TerminalCompleteFixture:
    state_dir: Path
    paths: Mapping[str, Path]
    all_paths: Mapping[str, Path]
    ownership_ledger: OwnershipLedger
    requested_images: RuntimeImages
    intent: StateUpgradeIntent


def _terminal_complete_fixture(tmp_path: Path) -> TerminalCompleteFixture:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    requested_images = replace(
        desired.runtime_images,
        redis="docker.io/library/redis@sha256:" + "e" * 64,
    )
    paths = state_upgrade_paths(tmp_path)
    all_paths = {**paths, "owner": tmp_path / "shared-core-sync-owner.json"}
    pre_desired = desired.to_dict()
    post_desired = {**pre_desired, "openclaw_gateway_token": "terminal-stale-token"}
    pre_desired_hash = sha256(
        json.dumps(pre_desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    ledger = OwnershipLedger(format_version=desired.format_version, resources=())
    atomic_json(paths["raw_input"], raw.to_dict())
    atomic_json(paths["desired"], post_desired)
    atomic_json(
        paths["applied"],
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=pre_desired_hash,
            completed_steps=(),
            runtime_images=desired.runtime_images,
        ).to_dict(),
    )
    atomic_json(paths["ledger"], ledger.to_dict())
    atomic_json(all_paths["owner"], SyncOwner(owner_id=_OWNER).to_dict())
    post_hashes = {key: file_hash(path) for key, path in all_paths.items()}
    intent = StateUpgradeIntent(
        generation=7,
        cas_token="a" * 64,
        status="complete",
        owner_id=_OWNER,
        pre_hashes={**post_hashes, "desired": pre_desired_hash},
        post_hashes=post_hashes,
        completed_writes=WRITE_ORDER,
        created_at="2026-08-05T00:00:00+00:00",
        updated_at="2026-08-05T00:00:00+00:00",
    )
    _write_intent(tmp_path, intent)
    assert set(all_paths) == set(ALL_KEYS)
    assert all(post_hashes[key] == "MISSING" for key in ALL_KEYS[4:7])
    return TerminalCompleteFixture(tmp_path, paths, all_paths, ledger, requested_images, intent)


def _write_intent(state_dir: Path, intent: StateUpgradeIntent) -> None:
    atomic_json(state_dir / _INTENT, intent.to_dict())


def _upgrade(fixture: TerminalCompleteFixture) -> StateUpgradeIntent:
    return upgrade_state_contract(
        state_dir=fixture.state_dir,
        owner_id=_OWNER,
        paths=fixture.paths,
        runtime_images=fixture.requested_images,
        ownership_ledger=fixture.ownership_ledger,
    )


def _snapshot(fixture: TerminalCompleteFixture) -> tuple[dict[str, str], bytes]:
    return (
        {key: file_hash(path) for key, path in fixture.all_paths.items()},
        (fixture.state_dir / _INTENT).read_bytes(),
    )


def _assert_unchanged(
    fixture: TerminalCompleteFixture, before: tuple[dict[str, str], bytes]
) -> None:
    assert {key: file_hash(path) for key, path in fixture.all_paths.items()} == before[0]
    assert (fixture.state_dir / _INTENT).read_bytes() == before[1]


def _different_hash(value: str) -> str:
    return "0" * 64 if value != "0" * 64 else "1" * 64


def test_complete_intent_restarts_generation_with_requested_runtime_images(tmp_path: Path) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    expected_fingerprint = replace(
        parse_desired_state_payload(read_json(fixture.paths["desired"])),
        runtime_images=fixture.requested_images,
    ).fingerprint()

    completed = _upgrade(fixture)

    final_intent = StateUpgradeIntent.from_dict(read_json(tmp_path / _INTENT))
    final_desired = parse_desired_state_payload(read_json(fixture.paths["desired"]))
    final_applied = AppliedStateCheckpoint.from_dict(read_json(fixture.paths["applied"]))
    assert completed.generation > fixture.intent.generation
    assert final_intent.generation == completed.generation
    assert final_desired.runtime_images == fixture.requested_images
    assert final_applied.runtime_images == fixture.requested_images
    assert final_desired.fingerprint() == expected_fingerprint
    assert final_applied.desired_state_fingerprint == expected_fingerprint


def test_complete_intent_rejects_foreign_owner_without_mutation(tmp_path: Path) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    _write_intent(
        fixture.state_dir,
        replace(fixture.intent, owner_id="6be4d0c1-e2a2-4dbb-97ed-c3d52e7680de"),
    )
    before = _snapshot(fixture)

    with pytest.raises(StateUpgradeError, match="complete intent owner does not match"):
        _upgrade(fixture)

    _assert_unchanged(fixture, before)


def test_complete_intent_rejects_incomplete_paths_before_indexing(tmp_path: Path) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    incomplete_paths = {key: path for key, path in fixture.all_paths.items() if key != "owner"}
    request = RuntimeProjectionRequest(
        fixture.state_dir,
        _OWNER,
        incomplete_paths,
        fixture.requested_images,
        fixture.ownership_ledger,
    )
    before = _snapshot(fixture)

    with pytest.raises(StateUpgradeError, match="complete intent paths are incomplete"):
        runtime_complete_intent_target_documents(request, fixture.intent)

    _assert_unchanged(fixture, before)


def test_complete_intent_rejects_current_postimage_drift_without_mutation(tmp_path: Path) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    serialized_intent = (tmp_path / _INTENT).read_bytes()
    atomic_json(
        fixture.paths["desired"],
        {**read_json(fixture.paths["desired"]), "openclaw_gateway_token": "postimage-drift"},
    )
    assert (tmp_path / _INTENT).read_bytes() == serialized_intent
    before = _snapshot(fixture)

    with pytest.raises(StateUpgradeError, match="complete intent bytes do not match"):
        _upgrade(fixture)

    _assert_unchanged(fixture, before)


def test_complete_intent_rejects_unchanged_desired_image_without_mutation(tmp_path: Path) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    _write_intent(
        tmp_path,
        replace(
            fixture.intent,
            pre_hashes={
                **fixture.intent.pre_hashes,
                "desired": fixture.intent.post_hashes["desired"],
            },
        ),
    )
    before = _snapshot(fixture)

    with pytest.raises(StateUpgradeError, match="does not bind the stale applied image"):
        _upgrade(fixture)

    _assert_unchanged(fixture, before)


def test_complete_intent_rejects_changed_applied_image_without_mutation(tmp_path: Path) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    _write_intent(
        tmp_path,
        replace(
            fixture.intent,
            pre_hashes={
                **fixture.intent.pre_hashes,
                "applied": _different_hash(fixture.intent.pre_hashes["applied"]),
            },
        ),
    )
    before = _snapshot(fixture)

    with pytest.raises(StateUpgradeError, match="does not bind the stale applied image"):
        _upgrade(fixture)

    _assert_unchanged(fixture, before)


def test_complete_intent_rejects_semantic_applied_mismatch_without_mutation(tmp_path: Path) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    applied = AppliedStateCheckpoint.from_dict(read_json(fixture.paths["applied"]))
    atomic_json(
        fixture.paths["applied"],
        replace(
            applied,
            desired_state_fingerprint=_different_hash(fixture.intent.pre_hashes["desired"]),
        ).to_dict(),
    )
    applied_hash = file_hash(fixture.paths["applied"])
    _write_intent(
        tmp_path,
        replace(
            fixture.intent,
            pre_hashes={**fixture.intent.pre_hashes, "applied": applied_hash},
            post_hashes={**fixture.intent.post_hashes, "applied": applied_hash},
        ),
    )
    before = _snapshot(fixture)

    with pytest.raises(StateUpgradeError, match="does not bind the stale applied image"):
        _upgrade(fixture)

    _assert_unchanged(fixture, before)


@pytest.mark.parametrize(
    "protected_key",
    ("raw_input", "litellm_keys", "surfsense_secrets", "seaweedfs_secrets"),
)
def test_complete_intent_rejects_protected_prehash_drift_without_mutation(
    tmp_path: Path, protected_key: str
) -> None:
    fixture = _terminal_complete_fixture(tmp_path)
    _write_intent(
        tmp_path,
        replace(
            fixture.intent,
            pre_hashes={
                **fixture.intent.pre_hashes,
                protected_key: _different_hash(fixture.intent.pre_hashes[protected_key]),
            },
        ),
    )
    before = _snapshot(fixture)

    with pytest.raises(StateUpgradeError, match="complete intent changed protected bytes"):
        _upgrade(fixture)

    _assert_unchanged(fixture, before)
