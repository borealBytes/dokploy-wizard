# ruff: noqa: E501, I001
"""CLI commands for the Task 1 live-baseline safety envelope."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Sequence

from dokploy_wizard.proof import ProofRecovery, ProofRecoveryPaths, atomic_finalize
from dokploy_wizard.proof.model_sync_artifacts import write_protected_manifest
from dokploy_wizard.proof.model_sync_baseline import parse_captured_baseline
from dokploy_wizard.proof.model_sync_env import (
    prepare_proof_env,
    resolve_proof_namespace,
    resolve_proof_transport,
)
from dokploy_wizard.proof.model_sync_host_a import (
    BaselineArtifactInputs,
    begin_proof_recovery,
    complete_resumable_finalization,
    finalize_baseline_artifacts,
    recover_interrupted_proof,
)
from dokploy_wizard.proof.model_sync_host_b import HostIdentity, assert_namespace_identity
from dokploy_wizard.proof.model_sync_remote import capture_host_a_snapshot, probe_host
from dokploy_wizard.proof.model_sync_results import run_bounded_process
from dokploy_wizard.proof.model_sync_state import (
    AbortGuard,
    AbortGuardError,
    process_start_time_ticks,
    read_abort_guard,
)

SignalHandler = Callable[[int, FrameType | None], object] | int | None


def main(argv: Sequence[str] | None = None) -> int:
    """Run a proof command without emitting supplied secret values."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "atomic-finalize":
                atomic_finalize(temp=args.temp, output=args.output)
            case "abort-status":
                status = read_abort_guard(args.guard)
                write_protected_manifest(args.output, _status_payload(status))
            case "baseline-host-a":
                _baseline_host_a(args)
            case _:
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model-sync-live-proof")
    commands = parser.add_subparsers(dest="command", required=True)
    finalize = commands.add_parser("atomic-finalize")
    finalize.add_argument("--temp", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    status = commands.add_parser("abort-status")
    status.add_argument("--guard", type=Path, required=True)
    status.add_argument("--output", type=Path, required=True)
    baseline = commands.add_parser("baseline-host-a")
    baseline.add_argument("--wrapper", type=Path, required=True)
    baseline.add_argument("--env-file", type=Path, required=True)
    baseline.add_argument("--external-backup", type=Path, required=True)
    baseline.add_argument("--abort-guard", type=Path, required=True)
    baseline.add_argument("--host-env", required=True)
    baseline.add_argument("--password-env", required=True)
    baseline.add_argument("--host-b-env", required=True)
    baseline.add_argument("--host-b-password-env", required=True)
    baseline.add_argument("--source-base-commit", required=True)
    baseline.add_argument("--proof-commit", required=True)
    baseline.add_argument("--artifact-dir", type=Path, required=True)
    baseline.add_argument("--output", type=Path, required=True)
    return parser


def _baseline_host_a(args: argparse.Namespace) -> None:
    host_a, password_a, host_b, password_b = _required_inputs(args)
    repository_root = _require_active_workspace_root(args.wrapper)
    recovery = begin_proof_recovery(
        paths=ProofRecoveryPaths(
            args.env_file,
            args.external_backup,
            args.abort_guard,
            args.artifact_dir,
            args.output,
            repository_root,
        ),
        pid=os.getpid(),
        start_time_ticks=_self_start_time_ticks(),
    )
    if recovery.terminal:
        return
    signal_state = {"critical": False, "completed": False, "pending": 0}
    previous_handlers = _install_recovery_handlers(recovery, signal_state)
    completed = False
    try:
        if recovery.resumable:
            signal_state["critical"] = True
            complete_resumable_finalization(recovery)
            completed = True
            signal_state["completed"] = True
            signal_state["critical"] = False
            if signal_state["pending"]:
                raise SystemExit(128 + signal_state["pending"])
            return
        if recovery.claim is None:
            raise RuntimeError("nonterminal proof recovery has no claim")
        prepared = prepare_proof_env(
            env_file=args.env_file,
            backup_path=args.external_backup,
            guard_path=args.abort_guard,
            claim_token=recovery.claim.token,
        )
        namespace = resolve_proof_namespace(args.env_file)
        transport = resolve_proof_transport(args.env_file)
        host_a_probe = probe_host(
            host=host_a, password=password_a, namespace=namespace, proof_transport=transport
        )
        host_b_probe = probe_host(
            host=host_b, password=password_b, namespace=namespace, proof_transport=transport
        )
        identity_a = HostIdentity(host_a_probe.machine_sha256, host_a_probe.ssh_sha256, host_a_probe.architecture)
        identity_b = HostIdentity(host_b_probe.machine_sha256, host_b_probe.ssh_sha256, host_b_probe.architecture)
        assert_namespace_identity(host_a=identity_a, host_b=identity_b)
        if not host_a_probe.namespace_clean or not host_b_probe.namespace_clean:
            raise RuntimeError("managed namespace residue blocks live baseline proof")
        _run_wrapper(args.wrapper, host_a, password_a, args.env_file)
        baseline = parse_captured_baseline(
            capture_host_a_snapshot(host=host_a, password=password_a),
            stack_name=namespace.stack_name,
        )
        signal_state["critical"] = True
        finalize_baseline_artifacts(
            BaselineArtifactInputs(
                repository_root=repository_root,
                artifact_dir=args.artifact_dir,
                output=args.output,
                source_base_commit=args.source_base_commit,
                proof_commit=args.proof_commit,
                prepared=prepared,
                guard_path=args.abort_guard,
                claim=recovery.claim,
                host_a=host_a_probe,
                host_b=host_b_probe,
                baseline=baseline,
            )
        )
        completed = True
        signal_state["completed"] = True
        signal_state["critical"] = False
        if signal_state["pending"]:
            raise SystemExit(128 + signal_state["pending"])
    finally:
        signal_state["critical"] = False
        _restore_recovery_handlers(previous_handlers)
        if not completed:
            recover_interrupted_proof(recovery)
def _required_inputs(args: argparse.Namespace) -> tuple[str, str, str, str]:
    names = (args.host_env, args.password_env, args.host_b_env, args.host_b_password_env)
    host_a = os.environ.get(args.host_env)
    password_a = os.environ.get(args.password_env)
    host_b = os.environ.get(args.host_b_env)
    password_b = os.environ.get(args.host_b_password_env)
    values = (host_a, password_a, host_b, password_b)
    if any(value is None or value == "" for value in values):
        missing = ", ".join(name for name, value in zip(names, values, strict=True) if not value)
        raise RuntimeError(f"missing required external inputs: {missing}")
    assert host_a is not None
    assert password_a is not None
    assert host_b is not None
    assert password_b is not None
    return host_a, password_a, host_b, password_b


def _require_active_workspace_root(wrapper: Path) -> Path:
    expected = Path("/workspaces/model-sync").resolve()
    if not expected.exists() or wrapper.resolve().parent != expected / "bin":
        raise RuntimeError("/workspaces/model-sync does not resolve to the active proof root")
    return expected


def _run_wrapper(wrapper: Path, host: str, password: str, env_file: Path) -> None:
    run_bounded_process(
        [
            str(wrapper),
            "proof",
            "--host",
            host,
            "--password-stdin",
            "--env-file",
            str(env_file),
        ],
        stdin=(password + "\n").encode(),
        output_limit=2 * 1024 * 1024,
        timeout_seconds=3600,
        label="remote proof wrapper",
    )
def _self_start_time_ticks() -> str:
    return process_start_time_ticks(Path("/proc/self/stat").read_text(encoding="utf-8"))
def _install_recovery_handlers(
    recovery: ProofRecovery, signal_state: dict[str, bool | int] | None = None
) -> tuple[SignalHandler, SignalHandler]:
    if signal_state is None:
        signal_state = {"critical": False, "completed": False, "pending": 0}
    recovering = False

    def restore(_signum: int, _frame: FrameType | None) -> None:
        nonlocal recovering
        if signal_state["completed"]:
            raise SystemExit(128 + _signum)
        if signal_state["critical"]:
            signal_state["pending"] = _signum
            return
        if recovering:
            return
        recovering = True
        recover_interrupted_proof(recovery)
        raise SystemExit(128 + _signum)

    return (
        signal.signal(signal.SIGINT, restore),
        signal.signal(signal.SIGTERM, restore),
    )


def _restore_recovery_handlers(
    previous: tuple[SignalHandler, SignalHandler],
) -> None:
    signal.signal(signal.SIGINT, previous[0])
    signal.signal(signal.SIGTERM, previous[1])


def _status_payload(status: AbortGuard) -> dict[str, str | int | None]:
    if status.phase != "complete" or status.claimant_kind != "plan":
        raise AbortGuardError("abort guard has unresolved recovery state")
    return {
        "state": status.state,
        "phase": status.phase,
        "claimant_kind": status.claimant_kind,
        "pid": status.pid,
        "start_time_ticks": status.start_time_ticks,
    }
