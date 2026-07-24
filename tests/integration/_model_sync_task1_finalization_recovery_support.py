from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts, model_sync_state
from dokploy_wizard.proof.model_sync_state import (
    arm_abort_guard,
    claim_abort_guard,
    read_abort_guard,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)
from dokploy_wizard.proof.model_sync_task1_context import derive_task1_proof_context
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    Task1FinalizationPlan,
)
from dokploy_wizard.proof.model_sync_task1_finalization_recovery import (
    Task1FinalizationRecoveryBoundary,
    persist_task1_finalization_plan,
)
from dokploy_wizard.proof.model_sync_task1_materialization import (
    materialize_task1_external_files,
)
from dokploy_wizard.proof.model_sync_task1_remote_abort import (
    record_remote_mutation_possible,
)
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    cleanup_report,
)


class InjectedCrash(RuntimeError):
    """Simulate an abrupt process exit at one durable recovery boundary."""


@dataclass(frozen=True, slots=True)
class Task1RecoveryFixture:
    """A claimed Task 1 proof with optional finalization state."""

    paths: proof.ProofRecoveryPaths
    host: str


def task1_recovery_fixture(tmp_path: Path, *, persist_plan: bool = True) -> Task1RecoveryFixture:
    source = tmp_path / ".install-min.env"
    source_bytes = (
        b"AI_DEFAULT_MODEL=example/model\nAI_DEFAULT_PROVIDER=openrouter\n"
        b"PACKS=coder\nROOT_DOMAIN=example.test\n"
    )
    source.write_bytes(source_bytes)
    source.chmod(0o600)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    root = tmp_path / "repository"
    protected = root / ".omo" / "evidence" / "unrelated.txt"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b"unrelated")
    manifest = model_sync_artifacts.protected_manifest_bytes(
        {".omo/evidence/unrelated.txt": hashlib.sha256(b"unrelated").hexdigest()}
    )
    model_sync_artifacts.atomic_write_bytes(
        artifact_dir / "protected-artifacts-before.txt", manifest
    )
    model_sync_artifacts.atomic_write_bytes(
        artifact_dir / "protected-artifacts-before.sha256",
        f"{hashlib.sha256(manifest).hexdigest()}  protected-artifacts-before.txt\n".encode(),
    )
    paths = proof.ProofRecoveryPaths(
        source,
        tmp_path / "backup.env",
        artifact_dir / "abort-guard.json",
        artifact_dir,
        artifact_dir / "result.json",
        root,
    )
    arm_abort_guard(paths.guard_path)
    claim_abort_guard(paths.guard_path, pid=123, start_time_ticks="456", claim_token="t" * 32)
    prepared = derive_task1_proof_context(
        source_values={
            "AI_DEFAULT_MODEL": "example/model",
            "AI_DEFAULT_PROVIDER": "openrouter",
            "PACKS": "coder",
            "ROOT_DOMAIN": "example.test",
        },
        source_bytes=source_bytes,
        source_path=source,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)
    paths.backup_path.write_bytes(source_bytes)
    paths.backup_path.chmod(0o600)
    evidence = prepared.evidence
    model_sync_state.record_env_intent(
        paths.guard_path,
        claim_token="t" * 32,
        receipt=proof.EnvReceipt(
            str(source.resolve()),
            str(paths.backup_path.resolve()),
            evidence.source_env_sha256,
            evidence.source_env_sha256,
            0o600,
            evidence,
        ),
    )
    model_sync_state.record_proof_active(paths.guard_path, claim_token="t" * 32)
    host = "fixture-host"
    if persist_plan:
        persist_task1_finalization_plan(paths.guard_path, host, plan())
    record_remote_mutation_possible(paths.guard_path, host)
    return Task1RecoveryFixture(paths, host)


def plan() -> Task1FinalizationPlan:
    return Task1FinalizationPlan(
        source_base_commit="a" * 40,
        proof_commit="b" * 40,
        host_identity_mode="distinct",
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
    )


def report(paths: proof.ProofRecoveryPaths) -> Task1CloudflareCleanupReport:
    receipt = read_abort_guard(paths.guard_path).env_receipt
    assert receipt is not None and receipt.context_evidence is not None
    return cleanup_report(receipt.context_evidence.context_sha256)


def restoration_report_bytes(paths: proof.ProofRecoveryPaths, status: str) -> bytes:
    cleanup = report(paths)
    receipt = (
        json.dumps(
            {
                "context_sha256": cleanup.pre_install.context_sha256,
                "operations_sha256": cleanup.final_journal_sha256,
                "status": status,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    value = {
        "cleanup_receipt": json.loads(receipt),
        "cleanup_receipt_sha256": hashlib.sha256(receipt).hexdigest(),
        "context_sha256": cleanup.pre_install.context_sha256,
        "final_journal_sha256": cleanup.final_journal_sha256,
        "operations_sha256": cleanup.final_journal_sha256,
        "post_cleanup_cloudflare": json.loads(cleanup.post_cleanup.to_bytes()),
        "post_cleanup_snapshot_sha256": cleanup.post_cleanup.snapshot_sha256,
        "post_install_cloudflare": json.loads(cleanup.post_install.to_bytes()),
        "post_install_snapshot_sha256": cleanup.post_install.snapshot_sha256,
        "pre_install_cloudflare": json.loads(cleanup.pre_install.to_bytes()),
        "pre_install_snapshot_sha256": cleanup.pre_install.snapshot_sha256,
        "status": status,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def runner_args(paths: proof.ProofRecoveryPaths) -> Namespace:
    return Namespace(
        single_host_sequential=False,
        host_env="host",
        password_env="password",
        host_b_env="host-b",
        host_b_password_env="password-b",
        env_file=paths.env_file,
        external_backup=paths.backup_path,
        abort_guard=paths.guard_path,
        artifact_dir=paths.artifact_dir,
        output=paths.output,
        active_root=paths.repository_root,
        task1_proof_context=True,
        wrapper=paths.artifact_dir / "wrapper",
        source_base_commit="a" * 40,
        proof_commit="b" * 40,
    )


def crash_at(
    boundary: Task1FinalizationRecoveryBoundary,
) -> Callable[[Task1FinalizationRecoveryBoundary | proof.FinalizationBoundary], None]:
    def hook(actual: Task1FinalizationRecoveryBoundary | proof.FinalizationBoundary) -> None:
        if actual is boundary:
            raise InjectedCrash(boundary)

    return hook
