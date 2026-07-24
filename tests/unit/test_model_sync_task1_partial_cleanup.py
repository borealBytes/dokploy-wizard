from __future__ import annotations

import os
from pathlib import Path

import pytest

from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_task1_context import derive_task1_proof_context
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError
from dokploy_wizard.proof.model_sync_task1_flow import (
    Task1ProofPreparationInputs,
    prepare_task1_proof_context,
)
from dokploy_wizard.proof.model_sync_task1_materialization import (
    Task1PreparationBoundary,
    materialize_task1_external_files,
)
from dokploy_wizard.proof.model_sync_task1_partial_cleanup import (
    cleanup_task1_partial_materialization,
)


class InjectedBackupFailure(RuntimeError):
    pass


def _source_bytes() -> bytes:
    return b"PACKS=coder,seaweedfs\nROOT_DOMAIN=example.test\n"


def test_unknown_backup_preserves_all_pre_active_material_for_inspection(
    tmp_path: Path,
) -> None:
    # Given
    env_file = tmp_path / ".install-min.env"
    env_file.write_bytes(_source_bytes())
    env_file.chmod(0o600)
    backup = tmp_path / "secrets" / "backup.env"
    guard = tmp_path / "guard.json"
    state.arm_abort_guard(guard)
    state.claim_abort_guard(
        guard,
        pid=os.getpid(),
        start_time_ticks=state.process_start_time_ticks(
            Path("/proc/self/stat").read_text(encoding="utf-8")
        ),
        claim_token="t" * 32,
    )
    inputs = Task1ProofPreparationInputs(env_file, backup, guard, "t" * 32)

    def corrupt_backup(boundary: Task1PreparationBoundary) -> None:
        if boundary.value == "backup-published":
            backup.write_bytes(b"unknown\n")
            raise InjectedBackupFailure

    # When / Then
    with pytest.raises(InjectedBackupFailure):
        prepare_task1_proof_context(inputs, boundary_hook=corrupt_backup)
    receipt = state.read_abort_guard(guard).env_receipt
    assert receipt is not None
    assert receipt.context_evidence is not None
    assert Path(receipt.context_evidence.proof_directory).is_dir()
    assert backup.read_bytes() == b"unknown\n"


def test_directory_identity_race_preserves_moved_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = tmp_path / ".install-min.env"
    source.write_bytes(_source_bytes())
    source.chmod(0o600)
    prepared = derive_task1_proof_context(
        source_values={"PACKS": "coder,seaweedfs", "ROOT_DOMAIN": "example.test"},
        source_bytes=_source_bytes(),
        source_path=source,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)
    original_unlink = artifacts.unlink_exact_regular_bytes
    moved = tmp_path / "moved-proof"
    raced = False

    def replace_directory(path: Path, expected: bytes) -> None:
        nonlocal raced
        if not raced:
            raced = True
            prepared.proof_directory.rename(moved)
            prepared.proof_directory.mkdir(mode=0o700)
        original_unlink(path, expected)

    monkeypatch.setattr(artifacts, "unlink_exact_regular_bytes", replace_directory)

    # When / Then
    with pytest.raises(Task1ProofContextError, match="directory changed"):
        cleanup_task1_partial_materialization(prepared.evidence, tmp_path / "absent-backup")
    assert prepared.proof_directory.is_dir()
    assert moved.is_dir()
    assert {path.name for path in moved.iterdir()} == {
        "upload.env",
        "task1-proof-context.json",
        "task1-proof-context.seed",
        "task1-proof-context.receipt.json",
    }
