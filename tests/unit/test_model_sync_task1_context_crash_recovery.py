from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_host_a import _recover_incomplete_guard
from dokploy_wizard.proof.model_sync_task1_context import derive_task1_proof_context
from dokploy_wizard.proof.model_sync_task1_evidence import (
    restore_task1_proof_source,
    verify_task1_external_evidence,
    verify_task1_finalized_evidence,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import (
    Task1ProofContextEvidenceV1,
)
from dokploy_wizard.proof.model_sync_task1_flow import (
    Task1ProofPreparationInputs,
    prepare_task1_proof_context,
)
from dokploy_wizard.proof.model_sync_task1_materialization import (
    Task1PreparationBoundary,
    materialize_task1_external_files,
)

_CLAIM_TOKEN = "t" * 32
_PRE_ACTIVE_BOUNDARIES = (
    "env-intent-recorded",
    "proof-directory-published",
    "upload-env-published",
    "context-published",
    "seed-published",
    "receipt-published",
    "backup-published",
    "before-proof-active",
)
UnknownMutation = Literal["bytes", "symlink", "special", "extra"]


class InjectedMaterializationFailure(RuntimeError):
    pass


def _source_bytes() -> bytes:
    return b"PACKS=coder,seaweedfs\nROOT_DOMAIN=example.test\n"


def _preparation_inputs(tmp_path: Path) -> Task1ProofPreparationInputs:
    env_file = tmp_path / ".install-min.env"
    env_file.write_bytes(_source_bytes())
    env_file.chmod(0o600)
    backup_path = tmp_path / "secrets" / "install.env.backup"
    guard_path = tmp_path / "abort-guard.json"
    state.arm_abort_guard(guard_path)
    state.claim_abort_guard(
        guard_path,
        pid=os.getpid(),
        start_time_ticks=state.process_start_time_ticks(
            Path("/proc/self/stat").read_text(encoding="utf-8")
        ),
        claim_token=_CLAIM_TOKEN,
    )
    return Task1ProofPreparationInputs(env_file, backup_path, guard_path, _CLAIM_TOKEN)


def _context_evidence(guard_path: Path) -> Task1ProofContextEvidenceV1:
    guard = state.read_abort_guard(guard_path)
    assert guard.env_receipt is not None
    assert guard.env_receipt.context_evidence is not None
    return guard.env_receipt.context_evidence


@pytest.mark.parametrize("target", _PRE_ACTIVE_BOUNDARIES)
def test_catchable_failure_removes_only_exact_pre_active_materialization(
    tmp_path: Path, target: str
) -> None:
    # Given
    inputs = _preparation_inputs(tmp_path)

    def interrupt(boundary: Task1PreparationBoundary) -> None:
        if boundary.value == target:
            raise InjectedMaterializationFailure(target)

    # When / Then
    with pytest.raises(InjectedMaterializationFailure, match=target):
        prepare_task1_proof_context(inputs, boundary_hook=interrupt)
    guard = state.read_abort_guard(inputs.guard_path)
    assert guard.phase == "env_intent"
    evidence = _context_evidence(inputs.guard_path)
    assert not os.path.lexists(evidence.proof_directory)
    assert not os.path.lexists(inputs.backup_path)
    assert inputs.env_file.read_bytes() == _source_bytes()


@pytest.mark.parametrize("mutation", ["bytes", "symlink", "special", "extra"])
def test_catchable_failure_preserves_unknown_pre_active_material(
    tmp_path: Path, mutation: UnknownMutation
) -> None:
    # Given
    inputs = _preparation_inputs(tmp_path)

    def corrupt_then_interrupt(boundary: Task1PreparationBoundary) -> None:
        if boundary.value != "upload-env-published":
            return
        evidence = _context_evidence(inputs.guard_path)
        upload = Path(evidence.upload_env_path)
        match mutation:
            case "bytes":
                upload.write_bytes(b"unknown\n")
            case "symlink":
                target = tmp_path / "unknown-target"
                target.write_bytes(upload.read_bytes())
                target.chmod(0o600)
                upload.unlink()
                upload.symlink_to(target)
            case "special":
                upload.unlink()
                os.mkfifo(upload, mode=0o600)
            case "extra":
                extra = Path(evidence.proof_directory) / "unknown-extra"
                extra.write_bytes(b"unknown\n")
                extra.chmod(0o600)
            case unreachable:
                assert_never(unreachable)
        raise InjectedMaterializationFailure(mutation)

    # When / Then
    with pytest.raises(InjectedMaterializationFailure, match=mutation):
        prepare_task1_proof_context(inputs, boundary_hook=corrupt_then_interrupt)
    evidence = _context_evidence(inputs.guard_path)
    assert os.path.lexists(evidence.proof_directory)
    assert os.path.lexists(evidence.upload_env_path)
    assert not os.path.lexists(inputs.backup_path)


@pytest.mark.parametrize("target", (*_PRE_ACTIVE_BOUNDARIES, "proof-active-recorded"))
def test_sigkill_recovery_converges_each_external_publication_boundary(
    tmp_path: Path, target: str
) -> None:
    # Given
    env_file = tmp_path / ".install-min.env"
    env_file.write_bytes(_source_bytes())
    env_file.chmod(0o600)
    backup_path = tmp_path / "secrets" / "install.env.backup"
    guard_path = tmp_path / "abort-guard.json"
    child = r"""
import os
import sys
from pathlib import Path
from dokploy_wizard.proof import model_sync_state as state
from dokploy_wizard.proof.model_sync_task1_flow import (
    Task1ProofPreparationInputs,
    prepare_task1_proof_context,
)

target, env_name, backup_name, guard_name = sys.argv[1:]
guard = Path(guard_name)
state.arm_abort_guard(guard)
state.claim_abort_guard(
    guard,
    pid=os.getpid(),
    start_time_ticks=state.process_start_time_ticks(
        Path("/proc/self/stat").read_text(encoding="utf-8")
    ),
    claim_token="t" * 32,
)

def pause(boundary):
    if boundary.value != target:
        return
    print(boundary.value, flush=True)
    sys.stdin.buffer.read(1)

prepare_task1_proof_context(
    Task1ProofPreparationInputs(Path(env_name), Path(backup_name), guard, "t" * 32),
    boundary_hook=pause,
)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child, target, str(env_file), str(backup_path), str(guard_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert process.stdout is not None
    assert process.stderr is not None
    observed = process.stdout.readline().strip()
    if observed != target:
        failure = process.stderr.read()
        process.wait(timeout=10)
        pytest.fail(f"child did not reach {target}: {failure}")
    process.send_signal(signal.SIGKILL)
    _stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == -signal.SIGKILL, stderr
    guard = state.read_abort_guard(guard_path)
    evidence = _context_evidence(guard_path)
    paths = proof.ProofRecoveryPaths(
        env_file,
        backup_path,
        guard_path,
        tmp_path / "artifacts",
        tmp_path / "artifacts" / "result.json",
        tmp_path / "repository",
    )

    # When
    _recover_incomplete_guard(
        paths,
        os.getpid(),
        state.process_start_time_ticks(Path("/proc/self/stat").read_text(encoding="utf-8")),
        guard,
    )

    # Then
    assert state.read_abort_guard(guard_path).phase == "ready"
    assert not os.path.lexists(backup_path)
    assert env_file.read_bytes() == _source_bytes()
    if target == "proof-active-recorded":
        verify_task1_external_evidence(evidence)
    else:
        assert not os.path.lexists(evidence.proof_directory)


def test_finalized_evidence_rejects_same_bytes_at_a_different_source_path(
    tmp_path: Path,
) -> None:
    # Given
    source_path = tmp_path / ".install-min.env"
    source_path.write_bytes(_source_bytes())
    source_path.chmod(0o600)
    prepared = derive_task1_proof_context(
        source_values={"PACKS": "coder,seaweedfs", "ROOT_DOMAIN": "example.test"},
        source_bytes=_source_bytes(),
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)
    backup = tmp_path / "backup.env"
    backup.write_bytes(_source_bytes())
    backup.chmod(0o600)
    evidence = restore_task1_proof_source(prepared=prepared, backup_path=backup)
    different_path = tmp_path / "different.env"
    different_path.write_bytes(_source_bytes())
    different_path.chmod(0o600)

    # When / Then
    with pytest.raises(ValueError):
        verify_task1_finalized_evidence(
            evidence=evidence,
            source_path=different_path,
            backup_path=backup,
        )
