"""Small command and adapter seams kept separate from Task 1 orchestration."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_artifacts import write_protected_manifest
from dokploy_wizard.proof.model_sync_cli_wrapper import (
    ProofWrapperInvocation,
    run_proof_wrapper,
)
from dokploy_wizard.proof.model_sync_results import run_bounded_process
from dokploy_wizard.proof.model_sync_signal_recovery import (
    SignalHandler,
    install_recovery_handlers,
)
from dokploy_wizard.proof.model_sync_state import AbortGuardError, read_abort_guard


def execute(
    parser: argparse.ArgumentParser,
    argv: Sequence[str] | None,
    run_baseline_host_a: Callable[[argparse.Namespace], None],
) -> int:
    """Execute the bounded model-sync command surface without owning its orchestration."""
    args = parser.parse_args(argv)
    try:
        if args.command == "atomic-finalize":
            proof.atomic_finalize(temp=args.temp, output=args.output)
        if args.command == "abort-status":
            status = read_abort_guard(args.guard)
            write_protected_manifest(args.output, proof.abort_status_payload(status))
        if args.command == "baseline-host-a":
            run_baseline_host_a(args)
        if args.command not in {"atomic-finalize", "abort-status", "baseline-host-a"}:
            parser.error("unknown proof command")
    except (
        AbortGuardError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"model-sync proof failed: {error}", file=sys.stderr)
        return 1
    return 0


def run_wrapper(
    wrapper: Path,
    host: str,
    password: str,
    env_file: Path,
    task1_proof_context: Path | None = None,
) -> None:
    """Invoke the remote proof wrapper with bounded stdout and password stdin."""
    run_proof_wrapper(
        ProofWrapperInvocation(wrapper, host, password, env_file, task1_proof_context),
        run_bounded_process,
    )


def require_active_workspace_root(wrapper: Path, paths: proof.ProofRecoveryPaths) -> Path:
    """Validate the checked-out workspace root used for proof artifacts."""
    return proof.require_active_repository_root(wrapper, paths)


def install_handlers(
    recovery: proof.ProofRecovery,
    recover_interrupted: Callable[[proof.ProofRecovery], None],
    signal_state: dict[str, bool | int] | None = None,
) -> tuple[SignalHandler, SignalHandler]:
    """Install the process-local proof recovery signal handlers."""
    return install_recovery_handlers(recovery, recover_interrupted, signal_state)
