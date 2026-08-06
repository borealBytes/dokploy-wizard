from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
)
from dokploy_wizard.state.store import (
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.upgrade import state_upgrade_paths, upgrade_state_contract
from dokploy_wizard.state.upgrade_intent import (
    WRITE_ORDER,
    StateUpgradeError,
    StateUpgradeIntent,
)
from dokploy_wizard.state.upgrade_io import atomic_json, file_hash, read_json

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"
_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


def test_complete_intent_recovers_when_canonical_preimage_hash_is_not_semantic_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a production-written state and a terminal receipt after the desired write.
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    requested_images = replace(
        desired.runtime_images,
        redis="docker.io/library/redis@sha256:" + "e" * 64,
    )
    ledger = OwnershipLedger(format_version=desired.format_version, resources=())
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=(),
            runtime_images=desired.runtime_images,
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    original_atomic_json = atomic_json

    def interrupt_applied_write(path: Path, payload: dict[str, object]) -> None:
        if path.name == "applied-state.json":
            raise OSError("fixture interruption")
        original_atomic_json(path, payload)

    monkeypatch.setattr("dokploy_wizard.state.upgrade.atomic_json", interrupt_applied_write)
    with pytest.raises(OSError, match="fixture interruption"):
        upgrade_state_contract(
            state_dir=tmp_path,
            owner_id=_OWNER,
            paths=state_upgrade_paths(tmp_path),
            runtime_images=requested_images,
            ownership_ledger=ledger,
        )
    monkeypatch.setattr("dokploy_wizard.state.upgrade.atomic_json", original_atomic_json)
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = StateUpgradeIntent.from_dict(read_json(intent_path))
    terminal_intent = replace(intent, status="complete", completed_writes=WRITE_ORDER)
    atomic_json(intent_path, terminal_intent.to_dict())
    assert intent.pre_hashes["desired"] != desired.fingerprint()
    assert AppliedStateCheckpoint.from_dict(
        read_json(state_upgrade_paths(tmp_path)["applied"])
    ).desired_state_fingerprint == desired.fingerprint()

    # When: the production state-upgrade route reopens the terminal receipt.
    completed = upgrade_state_contract(
        state_dir=tmp_path,
        owner_id=_OWNER,
        paths=state_upgrade_paths(tmp_path),
        runtime_images=requested_images,
        ownership_ledger=ledger,
    )

    # Then: the runtime projection completes and binds the current desired semantics.
    applied = AppliedStateCheckpoint.from_dict(read_json(state_upgrade_paths(tmp_path)["applied"]))
    desired_payload = read_json(state_upgrade_paths(tmp_path)["desired"])
    expected_fingerprint = sha256(
        json.dumps(desired_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert completed.generation > terminal_intent.generation
    assert applied.desired_state_fingerprint == expected_fingerprint


def test_complete_intent_rejects_wrong_canonical_preimage_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = parse_env_file(_FIXTURE)
    desired = resolve_desired_state(raw)
    requested_images = replace(
        desired.runtime_images,
        redis="docker.io/library/redis@sha256:" + "e" * 64,
    )
    ledger = OwnershipLedger(format_version=desired.format_version, resources=())
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=(),
            runtime_images=desired.runtime_images,
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    original_atomic_json = atomic_json

    def interrupt_applied_write(path: Path, payload: dict[str, object]) -> None:
        if path.name == "applied-state.json":
            raise OSError("fixture interruption")
        original_atomic_json(path, payload)

    monkeypatch.setattr("dokploy_wizard.state.upgrade.atomic_json", interrupt_applied_write)
    with pytest.raises(OSError, match="fixture interruption"):
        upgrade_state_contract(
            state_dir=tmp_path,
            owner_id=_OWNER,
            paths=state_upgrade_paths(tmp_path),
            runtime_images=requested_images,
            ownership_ledger=ledger,
        )
    monkeypatch.setattr("dokploy_wizard.state.upgrade.atomic_json", original_atomic_json)
    paths = state_upgrade_paths(tmp_path)
    applied = AppliedStateCheckpoint.from_dict(read_json(paths["applied"]))
    wrong_fingerprint = "0" * 64 if desired.fingerprint() != "0" * 64 else "1" * 64
    atomic_json(
        paths["applied"],
        replace(applied, desired_state_fingerprint=wrong_fingerprint).to_dict(),
    )
    intent_path = tmp_path / "state-upgrade-intent-v1.json"
    intent = StateUpgradeIntent.from_dict(read_json(intent_path))
    applied_hash = file_hash(paths["applied"])
    terminal_intent = replace(
        intent,
        status="complete",
        pre_hashes={**intent.pre_hashes, "applied": applied_hash},
        post_hashes={**intent.post_hashes, "applied": applied_hash},
        completed_writes=WRITE_ORDER,
    )
    atomic_json(intent_path, terminal_intent.to_dict())
    before = ({path.name: file_hash(path) for path in paths.values()}, intent_path.read_bytes())

    with pytest.raises(StateUpgradeError, match="does not bind the stale applied image"):
        upgrade_state_contract(
            state_dir=tmp_path,
            owner_id=_OWNER,
            paths=paths,
            runtime_images=requested_images,
            ownership_ledger=ledger,
        )

    after = ({path.name: file_hash(path) for path in paths.values()}, intent_path.read_bytes())
    assert after == before
