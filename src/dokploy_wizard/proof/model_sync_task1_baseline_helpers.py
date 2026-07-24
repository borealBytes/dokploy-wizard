"""Shared Task 1 baseline setup and cleanup helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

from dokploy_wizard.proof import model_sync_task1_flow as task1_flow
from dokploy_wizard.proof.model_sync_env import PreparedEnv, ProofNamespace
from dokploy_wizard.proof.model_sync_state import AbortGuardError
from dokploy_wizard.proof.model_sync_task1_finalization_bundle import (
    clear_finalization_bundle,
)
from dokploy_wizard.proof.model_sync_task1_remote_abort import (
    clear_remote_abort_record,
)


def prepare_proof(
    args: argparse.Namespace, claim_token: str, task1_context_enabled: bool
) -> tuple[PreparedEnv, ProofNamespace, Path, Path | None, task1_flow.Task1ProofPreparation | None]:
    """Prepare the configured legacy or Task 1 external proof input shape."""
    from dokploy_wizard.proof import model_sync_cli as cli

    if task1_context_enabled:
        preparation = task1_flow.prepare_task1_proof_context(
            task1_flow.Task1ProofPreparationInputs(
                args.env_file, args.external_backup, args.abort_guard, claim_token
            )
        )
        return (
            preparation.prepared_env,
            cli.resolve_task1_proof_namespace(preparation),
            preparation.external.uploaded_env_file,
            preparation.external.context_file,
            preparation,
        )
    return (
        cli.prepare_proof_env(
            env_file=args.env_file,
            backup_path=args.external_backup,
            guard_path=args.abort_guard,
            claim_token=claim_token,
        ),
        cli.resolve_proof_namespace(args.env_file),
        args.env_file,
        None,
        None,
    )


def cleanup_report_bytes(
    args: argparse.Namespace,
    host: str,
    password: str,
    preparation: task1_flow.Task1ProofPreparation | None,
) -> bytes:
    """Collect bounded remote cleanup output from the exact prepared context."""
    if preparation is None:
        raise AbortGuardError("Task 1 remote cleanup has no prepared external context")
    return task1_flow.cleanup_task1_cloudflare_proof(
        wrapper=args.wrapper, host=host, password=password, preparation=preparation
    )


def clear_recovery_sidecars(guard_path: Path, host: str) -> None:
    """Remove exact bundle and abort sidecars after local finalization succeeds."""
    clear_finalization_bundle(guard_path, host)
    clear_remote_abort_record(guard_path, host)
