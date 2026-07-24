from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_cli, model_sync_state
from dokploy_wizard.proof import model_sync_task1_baseline_resume as baseline_resume
from dokploy_wizard.proof import model_sync_task1_baseline_runner as baseline_runner
from dokploy_wizard.proof import model_sync_task1_flow as task1_flow
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_env import PreparedEnv
from dokploy_wizard.proof.model_sync_identity import ObservedResource, RemoteProbe
from dokploy_wizard.proof.model_sync_state import read_abort_guard
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    finalization_bundle_path,
)
from dokploy_wizard.proof.model_sync_task1_finalization_recovery import (
    Task1FinalizationRecoveryBoundary,
    bind_validated_remote_cleanup,
    resume_remote_cleanup_finalization,
)
from dokploy_wizard.proof.model_sync_task1_remote_abort import remote_abort_path
from tests.integration._model_sync_task1_finalization_recovery_support import (
    InjectedCrash,
    crash_at,
    plan,
    report,
    restoration_report_bytes,
    runner_args,
    task1_recovery_fixture,
)


@pytest.mark.parametrize(
    ("boundary", "phase"),
    [
        (Task1FinalizationRecoveryBoundary.BEFORE_FINALIZE_INTENT, "env_restored"),
        (Task1FinalizationRecoveryBoundary.AFTER_FINALIZE_INTENT, "finalize_intent"),
    ],
)
def test_local_finalization_crashes_resume_without_remote_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: Task1FinalizationRecoveryBoundary,
    phase: str,
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)
    bind_validated_remote_cleanup(fixture.paths.guard_path, fixture.host, report(fixture.paths))

    # When
    with pytest.raises(InjectedCrash):
        resume_remote_cleanup_finalization(
            fixture.paths,
            fixture.host,
            os.getpid(),
            proof.self_start_time_ticks(),
            boundary_hook=crash_at(boundary),
        )

    # Then
    assert read_abort_guard(fixture.paths.guard_path).phase == phase
    monkeypatch.setattr(model_sync_state, "process_identity_matches", lambda *_args: False)
    assert resume_remote_cleanup_finalization(
        fixture.paths,
        fixture.host,
        os.getpid(),
        proof.self_start_time_ticks(),
    )
    assert read_abort_guard(fixture.paths.guard_path).phase == "complete"


def test_pending_bundle_restored_cleanup_aborts_without_local_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)
    monkeypatch.setattr(model_sync_state, "process_identity_matches", lambda *_args: False)
    monkeypatch.setattr(
        task1_flow,
        "cleanup_task1_cloudflare_external",
        lambda **_kwargs: restoration_report_bytes(fixture.paths, "restored"),
    )
    inputs = baseline_resume.Task1ResumeInputs(
        runner_args(fixture.paths), fixture.paths, fixture.host, "pw"
    )

    # When / Then
    with pytest.raises(proof.AbortGuardError, match="safely aborted"):
        baseline_resume.resume_completed_cleanup(inputs)
    assert read_abort_guard(fixture.paths.guard_path).phase == "ready"
    assert not fixture.paths.output.exists()
    assert not finalization_bundle_path(fixture.paths.guard_path).exists()
    assert not remote_abort_path(fixture.paths.guard_path).exists()


def test_cleanup_retry_finalizes_in_same_invocation_without_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)
    paths = fixture.paths
    receipt = read_abort_guard(paths.guard_path).env_receipt
    assert receipt is not None
    recovery = proof.ProofRecovery(paths, proof.GuardClaim("t" * 32, 123, "456"), False, False)
    planes: dict[str, tuple[ObservedResource, ...]] = {
        name: () for name in ("cloudflare", "coder", "docker", "dokploy", "tailscale")
    }
    probe = RemoteProbe(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "amd64",
        True,
        planes,
        {name: "absent" for name in planes},
    )
    baseline = CapturedBaseline({}, plan().images, "6" * 64, "7" * 64)
    cleanup_calls = 0
    wrapper_calls: list[tuple[object, ...]] = []

    def cleanup(*_args: object) -> bytes:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("cleanup transport failure")
        return restoration_report_bytes(paths, "cleaned")

    monkeypatch.setattr(
        model_sync_cli,
        "resolve_host_inputs",
        lambda *_args: (fixture.host, "pw", fixture.host, "pw"),
    )
    monkeypatch.setattr(model_sync_cli, "begin_proof_recovery", lambda **_kwargs: recovery)
    monkeypatch.setattr(
        baseline_runner,
        "_prepare_proof",
        lambda *_args: (
            PreparedEnv(
                paths.env_file,
                paths.backup_path,
                receipt.original_sha256,
                receipt.proof_sha256,
                0o600,
            ),
            SimpleNamespace(stack_name="proof-stack"),
            paths.env_file,
            None,
            SimpleNamespace(external=None),
        ),
    )
    monkeypatch.setattr(model_sync_cli, "resolve_proof_transport", lambda *_args: None)
    monkeypatch.setattr(model_sync_cli, "probe_baseline_hosts", lambda **_kwargs: (probe, probe))
    monkeypatch.setattr(model_sync_cli, "probe_host", lambda **_kwargs: probe)
    monkeypatch.setattr(model_sync_cli, "classify_post_install_resources", lambda *_args: ())
    monkeypatch.setattr(model_sync_cli, "capture_host_a_snapshot", lambda **_kwargs: "{}")
    monkeypatch.setattr(
        model_sync_cli, "parse_captured_baseline", lambda *_args, **_kwargs: baseline
    )
    monkeypatch.setattr(
        model_sync_cli, "_require_active_workspace_root", lambda *_args: paths.repository_root
    )
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", lambda *_args: wrapper_calls.append(_args))
    monkeypatch.setattr(baseline_runner, "record_remote_mutation_possible", lambda *_args: None)
    monkeypatch.setattr(baseline_runner, "resume_completed_cleanup", lambda _inputs: False)
    monkeypatch.setattr(baseline_runner, "_cleanup_report", cleanup)

    # When
    baseline_runner.run_baseline_host_a(runner_args(paths))

    # Then
    assert cleanup_calls == 2
    assert len(wrapper_calls) == 1
    assert read_abort_guard(paths.guard_path).phase == "complete"
