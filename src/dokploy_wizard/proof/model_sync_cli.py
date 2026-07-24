"""CLI facade for Task 1 proof orchestration and local recovery."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_cli_commands as commands
from dokploy_wizard.proof import model_sync_signal_recovery as signal_recovery
from dokploy_wizard.proof import model_sync_task1_baseline_runner as baseline_runner
from dokploy_wizard.proof import model_sync_task1_flow as task1_flow
from dokploy_wizard.proof.model_sync_baseline import parse_captured_baseline
from dokploy_wizard.proof.model_sync_cli_wrapper import (
    ProofWrapperInvocation,
    run_proof_wrapper,
)
from dokploy_wizard.proof.model_sync_cloudflare_probe import classify_post_install_resources
from dokploy_wizard.proof.model_sync_env import (
    prepare_proof_env,
    resolve_proof_namespace,
    resolve_proof_transport,
)
from dokploy_wizard.proof.model_sync_finalization import finalize_baseline_artifacts
from dokploy_wizard.proof.model_sync_host_a import (
    begin_proof_recovery,
    complete_resumable_finalization,
    recover_interrupted_proof,
)
from dokploy_wizard.proof.model_sync_host_inputs import resolve_host_inputs
from dokploy_wizard.proof.model_sync_host_probes import probe_baseline_hosts
from dokploy_wizard.proof.model_sync_remote import capture_host_a_snapshot, probe_host
from dokploy_wizard.proof.model_sync_results import run_bounded_process
from dokploy_wizard.proof.model_sync_signal_recovery import SignalHandler

__all__ = (
    "_require_active_workspace_root",
    "_restore_recovery_handlers",
    "_run_wrapper",
    "_self_start_time_ticks",
    "begin_proof_recovery",
    "capture_host_a_snapshot",
    "classify_post_install_resources",
    "complete_resumable_finalization",
    "finalize_baseline_artifacts",
    "parse_captured_baseline",
    "prepare_proof_env",
    "probe_baseline_hosts",
    "probe_host",
    "recover_interrupted_proof",
    "resolve_host_inputs",
    "resolve_proof_namespace",
    "resolve_proof_transport",
    "resolve_task1_proof_namespace",
    "run_bounded_process",
    "signal_recovery",
)

_build_parser = proof.build_model_sync_parser
_self_start_time_ticks = proof.self_start_time_ticks
_restore_recovery_handlers = signal_recovery.restore_recovery_handlers
_require_active_workspace_root = commands.require_active_workspace_root
resolve_task1_proof_namespace = task1_flow.resolve_task1_proof_namespace


def main(argv: Sequence[str] | None = None) -> int:
    """Run one bounded model-sync proof command without printing supplied secrets."""
    return commands.execute(_build_parser(), argv, _baseline_host_a)


def _baseline_host_a(args: argparse.Namespace) -> None:
    """Delegate baseline execution to the recovery-aware Task 1 orchestration module."""
    baseline_runner.run_baseline_host_a(args)


def _run_wrapper(
    wrapper: Path,
    host: str,
    password: str,
    env_file: Path,
    task1_proof_context: Path | None = None,
) -> None:
    """Run the remote wrapper through the CLI-patchable bounded process seam."""
    run_proof_wrapper(
        ProofWrapperInvocation(wrapper, host, password, env_file, task1_proof_context),
        run_bounded_process,
    )


def _install_recovery_handlers(
    recovery: proof.ProofRecovery, signal_state: dict[str, bool | int] | None = None
) -> tuple[SignalHandler, SignalHandler]:
    """Install process-local handlers while keeping the CLI module patchable in tests."""
    return commands.install_handlers(recovery, recover_interrupted_proof, signal_state)
