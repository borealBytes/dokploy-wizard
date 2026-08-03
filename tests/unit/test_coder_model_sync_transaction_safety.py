from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.workspace_catalog_sync import (
    CatalogTarget,
    ModelCatalog,
    ProcessIdentity,
    TransactionBlockedError,
    WorkspaceCatalogTransaction,
    adapter_plan,
)
from tests.unit import test_coder_model_sync_adoption as adoption_fixtures
from tests.unit._kdense_catalog_fixtures import catalog_model_for
from tests.unit.test_coder_model_sync_rejections import _ProcessController


class _CrashProcessController(_ProcessController):
    def __init__(self) -> None:
        super().__init__()
        self.stop_actions = 0
        self.start_actions = 0
        self.crash_after_stop = False
        self.crash_after_start = False

    def stop(self, pid: int) -> None:
        self.stop_actions += 1
        super().stop(pid)
        if self.crash_after_stop:
            self.crash_after_stop = False
            raise RuntimeError("injected stop action crash")

    def start(self, previous: ProcessIdentity) -> int:
        self.start_actions += 1
        pid = super().start(previous)
        if self.crash_after_start:
            self.crash_after_start = False
            raise RuntimeError("injected start action crash")
        return pid


@pytest.fixture
def crash_process_control() -> Generator[_CrashProcessController, None, None]:
    controller = _CrashProcessController()
    yield controller
    controller.close()


def _catalog(*, credential_environment: str = "OPENAI_API_KEY") -> ModelCatalog:
    return ModelCatalog(
        base_url="http://stack-shared-litellm:4000/v1",
        credential_environment=credential_environment,
        credential_value_sha256="a" * 64,
        models=(catalog_model_for(credential_environment),),
    )


def test_pointer_transaction_rebases_unrelated_edit_before_prepare(tmp_path: Path) -> None:
    # Given
    path = tmp_path / ".config/opencode/opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"provider":{"litellm":{"models":{"old":{}}}},"unrelated":"one"}\n')
    target = adapter_plan("primary", tmp_path, _catalog()).targets[0]
    path.write_text('{"provider":{"litellm":{"models":{"old":{}}}},"unrelated":"two"}\n')
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=51)

    # When
    prepared = transaction.prepare(targets=(target,), explicit_operator_update=True)
    committed = transaction.commit(cas_token=prepared.cas_token)

    # Then
    document = json.loads(path.read_bytes())
    assert committed.phase == "committed"
    assert document["unrelated"] == "two"
    assert set(document["provider"]["litellm"]["models"]) == {"opencode-go/deepseek"}


def test_pointer_transaction_rejects_owned_edit_before_prepare(tmp_path: Path) -> None:
    # Given
    path = tmp_path / ".config/opencode/opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"provider":{"litellm":{"models":{"old":{}}}},"unrelated":"keep"}\n')
    target = adapter_plan("primary", tmp_path, _catalog()).targets[0]
    conflict = b'{"provider":{"litellm":{"models":{"user-edit":{}}}},"unrelated":"keep"}\n'
    path.write_bytes(conflict)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=52)

    # When / Then
    with pytest.raises(TransactionBlockedError, match="owned pointer"):
        transaction.prepare(targets=(target,), explicit_operator_update=True)
    assert path.read_bytes() == conflict


def test_staged_parent_symlink_swap_is_rejected_before_destination_write(
    tmp_path: Path,
) -> None:
    # Given
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=53)
    target = adapter_plan("primary", tmp_path, _catalog()).targets[0]
    prepared = transaction.prepare(targets=(target,))
    staged = transaction.transaction_dir / "staged"
    moved = transaction.transaction_dir / "staged-original"
    staged.rename(moved)
    attacker = tmp_path / "attacker-staged"
    attacker.mkdir()
    forged = attacker / "0.bin"
    forged.write_bytes((moved / "0.bin").read_bytes())
    forged.chmod(0o600)
    staged.symlink_to(attacker, target_is_directory=True)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.commit(cas_token=prepared.cas_token)
    assert not target.path.exists()


def test_preimage_parent_symlink_swap_is_rejected_before_rollback_restore(
    tmp_path: Path,
) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b"before")
    plan_target = CatalogTarget.file(path=target, content=b"managed", mode=0o600)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=54)
    prepared = transaction.prepare(targets=(plan_target,), explicit_operator_update=True)
    with pytest.raises(RuntimeError, match="injected crash"):
        transaction.commit(cas_token=prepared.cas_token, crash_after="files_written")
    preimages = transaction.transaction_dir / "preimages"
    moved = transaction.transaction_dir / "preimages-original"
    preimages.rename(moved)
    attacker = tmp_path / "attacker-preimages"
    attacker.mkdir()
    forged = attacker / "0.bin"
    forged.write_bytes(b"before")
    forged.chmod(0o600)
    preimages.symlink_to(attacker, target_is_directory=True)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.recover(cas_token=transaction.current().cas_token)
    assert target.read_bytes() == b"managed"
    preimages.unlink()
    moved.rename(preimages)
    recovered = transaction.recover(cas_token=transaction.current().cas_token)
    assert recovered.phase == "rolled_back"
    assert target.read_bytes() == b"before"


def test_destination_parent_symlink_swap_never_writes_attacker_target(
    tmp_path: Path,
) -> None:
    # Given
    parent = tmp_path / "authorized"
    parent.mkdir()
    target = parent / "config.json"
    target.write_bytes(b"before")
    managed = CatalogTarget.file(path=target, content=b"managed", mode=0o600)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=55)
    prepared = transaction.prepare(targets=(managed,), explicit_operator_update=True)
    moved = tmp_path / "authorized-original"
    parent.rename(moved)
    attacker = tmp_path / "attacker-target"
    attacker.mkdir()
    outside = attacker / "config.json"
    outside.write_bytes(b"before")
    parent.symlink_to(attacker, target_is_directory=True)

    # When / Then
    with pytest.raises(TransactionBlockedError):
        transaction.commit(cas_token=prepared.cas_token)
    assert outside.read_bytes() == b"before"


def test_destination_leaf_race_is_cas_rejected_without_overwriting_user_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    target = tmp_path / "config.json"
    target.write_bytes(b"before")
    managed = CatalogTarget.file(path=target, content=b"managed", mode=0o600)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=59)
    prepared = transaction.prepare(targets=(managed,), explicit_operator_update=True)
    real_fsync = os.fsync
    raced = False

    def race_after_temporary_fsync(descriptor: int) -> None:
        nonlocal raced
        real_fsync(descriptor)
        try:
            opened = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        except FileNotFoundError:
            return
        if not raced and opened.parent == target.parent and opened.name.startswith(
            ".config.json."
        ):
            target.write_bytes(b"user-race")
            raced = True

    monkeypatch.setattr(os, "fsync", race_after_temporary_fsync)

    # When / Then
    with pytest.raises(TransactionBlockedError, match="publication boundary"):
        transaction.commit(cas_token=prepared.cas_token)
    assert raced
    assert target.read_bytes() == b"user-race"


def test_adoption_receipt_race_is_rejected_without_replacing_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    target = tmp_path / "opencode.json"
    adoption_fixtures._write_pointer_target(target)
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=60)
    receipt = transaction.transaction_dir / "legacy-adoption-v1.json"
    real_fsync = os.fsync
    raced = False

    def race_after_temporary_fsync(descriptor: int) -> None:
        nonlocal raced
        real_fsync(descriptor)
        opened = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        if not raced and opened.name.startswith(".legacy-adoption-v1.json."):
            receipt.write_bytes(b"collision")
            raced = True

    monkeypatch.setattr(os, "fsync", race_after_temporary_fsync)

    # When / Then
    with pytest.raises(TransactionBlockedError, match="receipt already exists"):
        transaction.adopt_legacy(adoption_fixtures._pointer_request(target))
    assert raced
    assert receipt.read_bytes() == b"collision"


def test_kdense_nested_generation_commit_creates_only_authorized_parents(
    tmp_path: Path,
) -> None:
    # Given
    plan = adapter_plan(
        "kdense", tmp_path, _catalog(credential_environment="KDENSE_LITELLM_API_KEY")
    )
    generated = next(target for target in plan.targets if target.kind == "file")
    current = next(target for target in plan.targets if target.kind == "symlink")
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=56)
    prepared = transaction.prepare(targets=plan.targets)

    # When
    committed = transaction.commit(cas_token=prepared.cas_token)

    # Then
    assert committed.phase == "committed"
    assert generated.path.read_bytes() == generated.content
    assert current.path.resolve(strict=True) == generated.path.parent


def test_stop_action_crash_recovers_forward_without_repeating_signal(
    tmp_path: Path, crash_process_control: _CrashProcessController
) -> None:
    # Given
    target = tmp_path / "config.json"
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=57)
    previous = crash_process_control.observe(name="hermes", generation=57)
    managed = CatalogTarget.file(
        path=target, content=b"managed", mode=0o600
    )
    prepared = transaction.prepare(targets=(managed,), processes=(previous,))
    switched = transaction.commit(cas_token=prepared.cas_token)
    crash_process_control.crash_after_stop = True

    # When
    with pytest.raises(RuntimeError, match="stop action crash"):
        transaction.stop_processes(
            cas_token=switched.cas_token, control=crash_process_control
        )
    recovered = transaction.recover(
        cas_token=transaction.current().cas_token, control=crash_process_control
    )

    # Then
    assert recovered.phase == "committed"
    assert crash_process_control.stop_actions == 1
    assert crash_process_control.start_actions == 1


def test_start_action_crash_recovers_observed_replacement_without_restarting(
    tmp_path: Path, crash_process_control: _CrashProcessController
) -> None:
    # Given
    target = tmp_path / "config.json"
    transaction = WorkspaceCatalogTransaction(workspace_root=tmp_path, generation=58)
    previous = crash_process_control.observe(name="kdense", generation=58)
    managed = CatalogTarget.file(
        path=target, content=b"managed", mode=0o600
    )
    prepared = transaction.prepare(targets=(managed,), processes=(previous,))
    switched = transaction.commit(cas_token=prepared.cas_token)
    stopped = transaction.stop_processes(
        cas_token=switched.cas_token, control=crash_process_control
    )
    crash_process_control.crash_after_start = True

    # When
    with pytest.raises(RuntimeError, match="start action crash"):
        transaction.start_processes(
            cas_token=stopped.cas_token, control=crash_process_control
        )
    recovered = transaction.recover(
        cas_token=transaction.current().cas_token, control=crash_process_control
    )

    # Then
    assert recovered.phase == "committed"
    assert crash_process_control.stop_actions == 1
    assert crash_process_control.start_actions == 1
