from __future__ import annotations

from collections.abc import Callable
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
from dokploy_wizard.proof.model_sync_state import AbortGuardError, read_abort_guard
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    finalization_bundle_path,
)
from dokploy_wizard.proof.model_sync_task1_remote_abort import (
    clear_remote_abort_record,
    remote_abort_path,
)
from tests.integration._model_sync_task1_finalization_recovery_support import (
    Task1RecoveryFixture,
    plan,
    restoration_report_bytes,
    runner_args,
    task1_recovery_fixture,
)

ProofWrapperArgument = Path | str | None
ProofWrapperCall = tuple[ProofWrapperArgument, ...]


def _configure_pre_plan_failure(
    monkeypatch: pytest.MonkeyPatch,
    fixture: Task1RecoveryFixture,
    stage: str,
    cleanup_status: str,
    cleanup_calls: list[str],
    wrapper_calls: list[ProofWrapperCall],
) -> None:
    receipt = read_abort_guard(fixture.paths.guard_path).env_receipt
    assert receipt is not None
    recovery = proof.ProofRecovery(
        fixture.paths, proof.GuardClaim("t" * 32, 123, "456"), False, False
    )
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
                fixture.paths.env_file,
                fixture.paths.backup_path,
                receipt.original_sha256,
                receipt.proof_sha256,
                0o600,
            ),
            SimpleNamespace(stack_name="proof-stack"),
            fixture.paths.env_file,
            None,
            SimpleNamespace(external=None),
        ),
    )
    monkeypatch.setattr(model_sync_cli, "resolve_proof_transport", lambda *_args: None)
    monkeypatch.setattr(model_sync_cli, "probe_baseline_hosts", lambda **_kwargs: (probe, probe))

    def run_wrapper(*args: ProofWrapperArgument) -> None:
        wrapper_calls.append(args)
        if stage == "wrapper":
            raise RuntimeError("wrapper failed")

    def probe_host(**_kwargs: object) -> RemoteProbe:
        if stage == "probe":
            raise RuntimeError("probe failed")
        return probe

    def capture_host_a_snapshot(**_kwargs: object) -> str:
        if stage == "capture":
            raise RuntimeError("capture failed")
        return "{}"

    def cleanup_report(*_args: object) -> bytes:
        cleanup_calls.append(stage)
        return restoration_report_bytes(fixture.paths, cleanup_status)

    monkeypatch.setattr(model_sync_cli, "_run_wrapper", run_wrapper)
    monkeypatch.setattr(model_sync_cli, "probe_host", probe_host)
    monkeypatch.setattr(model_sync_cli, "classify_post_install_resources", lambda *_args: ())
    monkeypatch.setattr(
        model_sync_cli,
        "capture_host_a_snapshot",
        capture_host_a_snapshot,
    )
    monkeypatch.setattr(
        model_sync_cli,
        "parse_captured_baseline",
        lambda *_args, **_kwargs: CapturedBaseline({}, plan().images, "6" * 64, "7" * 64),
    )
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda *_args: fixture.paths.repository_root,
    )
    monkeypatch.setattr(baseline_runner, "_cleanup_report", cleanup_report)


@pytest.mark.parametrize("status", ["cleaned", "restored"])
def test_no_bundle_cleanup_restores_local_source_without_running_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path, persist_plan=False)
    source = fixture.paths.env_file.read_bytes()
    cleanup_calls: list[str] = []
    wrapper_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(model_sync_state, "process_identity_matches", lambda *_args: False)

    def cleanup(**_kwargs: object) -> bytes:
        cleanup_calls.append(status)
        return restoration_report_bytes(fixture.paths, status)

    def run_wrapper(*args: str) -> None:
        wrapper_calls.append(args)

    monkeypatch.setattr(task1_flow, "cleanup_task1_cloudflare_external", cleanup)
    monkeypatch.setattr(
        model_sync_cli,
        "resolve_host_inputs",
        lambda *_args: (fixture.host, "pw", fixture.host, "pw"),
    )
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda *_args: fixture.paths.repository_root,
    )
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", run_wrapper)

    # When / Then
    with pytest.raises(AbortGuardError, match="safely aborted"):
        baseline_runner.run_baseline_host_a(runner_args(fixture.paths))

    assert cleanup_calls == [status]
    assert wrapper_calls == []
    assert fixture.paths.env_file.read_bytes() == source
    assert read_abort_guard(fixture.paths.guard_path).phase == "ready"
    assert not fixture.paths.backup_path.exists()
    assert not finalization_bundle_path(fixture.paths.guard_path).exists()
    assert not remote_abort_path(fixture.paths.guard_path).exists()


def test_no_bundle_absent_journal_restores_local_source_without_running_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = task1_recovery_fixture(tmp_path, persist_plan=False)
    source = fixture.paths.env_file.read_bytes()
    wrapper_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(model_sync_state, "process_identity_matches", lambda *_args: False)

    def cleanup(**_kwargs: object) -> bytes:
        raise task1_flow.Task1CloudflareJournalAbsentError()

    def run_wrapper(*args: str) -> None:
        wrapper_calls.append(args)

    monkeypatch.setattr(task1_flow, "cleanup_task1_cloudflare_external", cleanup)
    monkeypatch.setattr(
        model_sync_cli,
        "resolve_host_inputs",
        lambda *_args: (fixture.host, "pw", fixture.host, "pw"),
    )
    monkeypatch.setattr(
        model_sync_cli,
        "_require_active_workspace_root",
        lambda *_args: fixture.paths.repository_root,
    )
    monkeypatch.setattr(model_sync_cli, "_run_wrapper", run_wrapper)

    with pytest.raises(AbortGuardError, match="safely aborted"):
        baseline_runner.run_baseline_host_a(runner_args(fixture.paths))

    assert wrapper_calls == []
    assert fixture.paths.env_file.read_bytes() == source
    assert read_abort_guard(fixture.paths.guard_path).phase == "ready"
    assert not fixture.paths.backup_path.exists()
    assert not finalization_bundle_path(fixture.paths.guard_path).exists()
    assert not remote_abort_path(fixture.paths.guard_path).exists()


def test_task1_cleanup_maps_absent_journal_without_exposing_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run(
        _command: list[str],
        *,
        nonzero_error_factory: Callable[[bytes], RuntimeError],
        **_kwargs: object,
    ) -> bytes:
        raise nonzero_error_factory(b"Task 1 Cloudflare cleanup journal is absent")

    monkeypatch.setattr(task1_flow, "run_bounded_process", run)

    with pytest.raises(task1_flow.Task1CloudflareJournalAbsentError):
        task1_flow.cleanup_task1_cloudflare_external(
            wrapper=tmp_path / "dokploy-wizard-remote",
            host="host",
            password="password",
            uploaded_env_file=tmp_path / "upload.env",
            context_file=tmp_path / "context.json",
        )


def test_no_bundle_cleanup_failure_preserves_recovery_for_one_safe_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path, persist_plan=False)
    calls = 0
    monkeypatch.setattr(model_sync_state, "process_identity_matches", lambda *_args: False)

    def cleanup(**_kwargs: str) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transport failed")
        return restoration_report_bytes(fixture.paths, "restored")

    monkeypatch.setattr(task1_flow, "cleanup_task1_cloudflare_external", cleanup)
    inputs = baseline_resume.Task1ResumeInputs(
        runner_args(fixture.paths), fixture.paths, fixture.host, "pw"
    )

    # When / Then
    with pytest.raises(RuntimeError, match="transport failed"):
        baseline_resume.resume_completed_cleanup(inputs)
    assert remote_abort_path(fixture.paths.guard_path).exists()
    assert read_abort_guard(fixture.paths.guard_path).phase == "proof_active"
    with pytest.raises(AbortGuardError, match="safely aborted"):
        baseline_resume.resume_completed_cleanup(inputs)

    assert calls == 2
    assert read_abort_guard(fixture.paths.guard_path).phase == "ready"
    assert not remote_abort_path(fixture.paths.guard_path).exists()


@pytest.mark.parametrize(
    ("stage", "cleanup_status"),
    [
        ("wrapper", "cleaned"),
        ("probe", "restored"),
        ("capture", "restored"),
    ],
)
def test_failure_before_finalization_plan_cleans_once_without_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, cleanup_status: str
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path, persist_plan=False)
    clear_remote_abort_record(fixture.paths.guard_path, fixture.host)
    source = fixture.paths.env_file.read_bytes()
    cleanup_calls: list[str] = []
    wrapper_calls: list[ProofWrapperCall] = []
    _configure_pre_plan_failure(
        monkeypatch, fixture, stage, cleanup_status, cleanup_calls, wrapper_calls
    )

    # When / Then
    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        baseline_runner.run_baseline_host_a(runner_args(fixture.paths))
    assert cleanup_calls == [stage]
    assert len(wrapper_calls) == 1
    assert fixture.paths.env_file.read_bytes() == source
    assert read_abort_guard(fixture.paths.guard_path).phase == "ready"
    assert not fixture.paths.backup_path.exists()
    assert not finalization_bundle_path(fixture.paths.guard_path).exists()
    assert not remote_abort_path(fixture.paths.guard_path).exists()
    assert not fixture.paths.output.exists()
