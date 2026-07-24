from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_state
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    FinalizationBundlePhase,
    Task1FinalizationBundle,
    finalization_bundle_path,
    require_finalization_bundle,
)
from dokploy_wizard.proof.model_sync_task1_finalization_recovery import (
    Task1FinalizationRecoveryBoundary,
    bind_validated_remote_cleanup,
    persist_task1_finalization_plan,
    resume_remote_cleanup_finalization,
)
from dokploy_wizard.proof.model_sync_task1_remote_abort import (
    Task1RemoteProofPhase,
    remote_abort_path,
    require_remote_abort_record,
)
from tests.integration._model_sync_task1_finalization_recovery_support import (
    InjectedCrash,
    crash_at,
    plan,
    report,
    task1_recovery_fixture,
)


def test_pending_bundle_binds_recovery_identity_before_remote_cleanup() -> None:
    # Given
    bundle = Task1FinalizationBundle(
        guard_id="a" * 64,
        context_sha256="b" * 64,
        uploaded_env_sha256="c" * 64,
        host_sha256="d" * 64,
        source_base_commit="e" * 40,
        proof_commit="f" * 40,
        host_identity_mode="distinct",
        phase=FinalizationBundlePhase.PENDING_CLEANUP,
        payloads={
            "baseline.json": b"{}\n",
            "host-a-preflight.json": b"{}\n",
            "host-b-preflight.json": b"{}\n",
        },
        images={
            "coder": "coder@sha256:" + "1" * 64,
            "litellm": "litellm@sha256:" + "2" * 64,
            "pgvector": "pgvector@sha256:" + "3" * 64,
            "redis": "redis@sha256:" + "4" * 64,
            "postfix": "postfix@sha256:" + "5" * 64,
        },
        coder_secret_inventory_sha256="6" * 64,
        legacy_workspace_managed_fingerprints_sha256="7" * 64,
        preexisting_cloudflare_sha256="8" * 64,
        post_install_cloudflare_sha256=None,
        snapshot_evidence=None,
    )

    # When
    content = bundle.to_bytes()

    # Then
    assert b'"phase":"pending_cleanup"' in content


def test_completed_remote_cleanup_resumes_from_proof_active_without_remote_work(
    tmp_path: Path,
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)
    bind_validated_remote_cleanup(fixture.paths.guard_path, fixture.host, report(fixture.paths))

    # When
    resumed = resume_remote_cleanup_finalization(
        fixture.paths,
        fixture.host,
        os.getpid(),
        proof.self_start_time_ticks(),
    )

    # Then
    assert resumed is True
    assert model_sync_state.read_abort_guard(fixture.paths.guard_path).phase == "complete"
    assert fixture.paths.output.exists()
    assert not finalization_bundle_path(fixture.paths.guard_path).exists()
    assert not remote_abort_path(fixture.paths.guard_path).exists()


@pytest.mark.parametrize(
    "boundary",
    [
        Task1FinalizationRecoveryBoundary.CLEANUP_VALIDATED,
        Task1FinalizationRecoveryBoundary.READY_BUNDLE_PUBLISHED,
    ],
)
def test_crash_before_remote_completion_keeps_recovery_state_retryable(
    tmp_path: Path,
    boundary: Task1FinalizationRecoveryBoundary,
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)

    # When
    with pytest.raises(InjectedCrash):
        bind_validated_remote_cleanup(
            fixture.paths.guard_path,
            fixture.host,
            report(fixture.paths),
            boundary_hook=crash_at(boundary),
        )

    # Then
    assert require_remote_abort_record(fixture.paths.guard_path, fixture.host).phase is (
        Task1RemoteProofPhase.REMOTE_MUTATION_POSSIBLE
    )
    bind_validated_remote_cleanup(fixture.paths.guard_path, fixture.host, report(fixture.paths))
    assert require_remote_abort_record(fixture.paths.guard_path, fixture.host).phase is (
        Task1RemoteProofPhase.REMOTE_CLEANUP_COMPLETE
    )


@pytest.mark.parametrize("tamper", ["bytes", "mode", "symlink", "extra"])
def test_recovery_bundle_rejects_tampering_before_local_finalization(
    tmp_path: Path, tamper: str
) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)
    bundle_path = finalization_bundle_path(fixture.paths.guard_path)

    # When
    match tamper:
        case "bytes":
            bundle_path.write_bytes(bundle_path.read_bytes() + b" ")
        case "mode":
            bundle_path.chmod(0o644)
        case "symlink":
            replacement = tmp_path / "replacement.json"
            replacement.write_bytes(bundle_path.read_bytes())
            replacement.chmod(0o600)
            bundle_path.unlink()
            bundle_path.symlink_to(replacement)
        case "extra":
            value = json.loads(bundle_path.read_bytes())
            value["unexpected"] = "field"
            bundle_path.write_bytes(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            )
        case unexpected:
            raise AssertionError(f"unexpected tamper mode {unexpected}")

    # Then
    with pytest.raises(proof.AbortGuardError):
        require_finalization_bundle(fixture.paths.guard_path, fixture.host)


def test_recovery_bundle_rejects_context_hash_mismatch(tmp_path: Path) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)
    bundle_path = finalization_bundle_path(fixture.paths.guard_path)
    value = json.loads(bundle_path.read_bytes())
    value["context_sha256"] = "0" * 64
    bundle_path.write_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )

    # When / Then
    with pytest.raises(proof.AbortGuardError):
        require_finalization_bundle(fixture.paths.guard_path, fixture.host)


def test_recovery_bundle_rejects_supplied_host_mismatch(tmp_path: Path) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path)

    # When / Then
    with pytest.raises(proof.AbortGuardError):
        require_finalization_bundle(fixture.paths.guard_path, "other-host")


def test_payload_build_crash_leaves_remote_cleanup_pending_without_sidecar(tmp_path: Path) -> None:
    # Given
    fixture = task1_recovery_fixture(tmp_path, persist_plan=False)

    # When
    with pytest.raises(InjectedCrash):
        persist_task1_finalization_plan(
            fixture.paths.guard_path,
            fixture.host,
            plan(),
            boundary_hook=crash_at(Task1FinalizationRecoveryBoundary.PAYLOADS_BUILT),
        )

    # Then
    assert not finalization_bundle_path(fixture.paths.guard_path).exists()
    assert require_remote_abort_record(fixture.paths.guard_path, fixture.host).phase is (
        Task1RemoteProofPhase.REMOTE_MUTATION_POSSIBLE
    )
