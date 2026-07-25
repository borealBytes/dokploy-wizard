"""Pre-guard resume path for a completed Task 1 remote cleanup."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_task1_flow as task1_flow
from dokploy_wizard.proof.model_sync_state import AbortGuardError, read_abort_guard
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    parse_cleanup_report,
    parse_restoration_report,
)
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    FinalizationBundlePhase,
    finalization_bundle_path,
    require_finalization_bundle,
)
from dokploy_wizard.proof.model_sync_task1_finalization_recovery import (
    abort_validated_remote_restoration,
    bind_validated_remote_cleanup,
    resume_remote_cleanup_finalization,
)
from dokploy_wizard.proof.model_sync_task1_finalization_recovery_helpers import (
    reclaim_task1_recovery,
)
from dokploy_wizard.proof.model_sync_task1_remote_abort import (
    Task1RemoteProofPhase,
    remote_abort_record_exists,
    require_remote_abort_record,
)


@dataclass(frozen=True, slots=True)
class Task1ResumeInputs:
    """Caller-bound inputs needed to resume cleanup without a new proof installation."""

    args: argparse.Namespace
    paths: proof.ProofRecoveryPaths
    host: str
    password: str


def resume_completed_cleanup(inputs: Task1ResumeInputs) -> bool:
    """Retry only pending cleanup, then complete local recovery before any normal guard reset."""
    if not remote_abort_record_exists(inputs.paths.guard_path):
        return False
    record = require_remote_abort_record(inputs.paths.guard_path, inputs.host)
    if record.phase is Task1RemoteProofPhase.REMOTE_MUTATION_POSSIBLE:
        if os.path.lexists(finalization_bundle_path(inputs.paths.guard_path)):
            bundle = require_finalization_bundle(inputs.paths.guard_path, inputs.host)
            match bundle.phase:
                case FinalizationBundlePhase.PENDING_CLEANUP:
                    cleanup_bytes = _cleanup_from_receipt(inputs)
                    match parse_restoration_report(cleanup_bytes).status:
                        case "cleaned":
                            bind_validated_remote_cleanup(
                                inputs.paths.guard_path,
                                inputs.host,
                                parse_cleanup_report(cleanup_bytes),
                            )
                        case "restored":
                            _abort_restored_cleanup(inputs, clear_bundle=True)
                case FinalizationBundlePhase.READY:
                    bind_validated_remote_cleanup(inputs.paths.guard_path, inputs.host, None)
        else:
            try:
                cleanup_bytes = _cleanup_from_receipt(inputs)
            except task1_flow.Task1CloudflareJournalAbsentError:
                _abort_prejournal_cleanup(inputs)
            parse_restoration_report(cleanup_bytes)
            _abort_restored_cleanup(inputs, clear_bundle=False)
    return resume_remote_cleanup_finalization(
        inputs.paths,
        inputs.host,
        os.getpid(),
        proof.self_start_time_ticks(),
    )


def _cleanup_from_receipt(inputs: Task1ResumeInputs) -> bytes:
    receipt = read_abort_guard(inputs.paths.guard_path).env_receipt
    if receipt is None or receipt.context_evidence is None:
        raise AbortGuardError("Task 1 remote cleanup record lacks context evidence")
    return task1_flow.cleanup_task1_cloudflare_external(
        wrapper=inputs.args.wrapper,
        host=inputs.host,
        password=inputs.password,
        uploaded_env_file=Path(receipt.context_evidence.upload_env_path),
        context_file=Path(receipt.context_evidence.context_path),
    )


def _abort_restored_cleanup(inputs: Task1ResumeInputs, *, clear_bundle: bool) -> None:
    recovery = reclaim_task1_recovery(inputs.paths, os.getpid(), proof.self_start_time_ticks())
    abort_validated_remote_restoration(recovery, inputs.host, clear_bundle=clear_bundle)
    raise AbortGuardError(
        "Task 1 previous attempt was safely aborted; recreate or clean the host before retrying"
    )


def _abort_prejournal_cleanup(inputs: Task1ResumeInputs) -> None:
    recovery = reclaim_task1_recovery(inputs.paths, os.getpid(), proof.self_start_time_ticks())
    abort_validated_remote_restoration(recovery, inputs.host, clear_bundle=False)
    raise AbortGuardError(
        "Task 1 previous attempt was safely aborted; recreate or clean the host before retrying"
    )
