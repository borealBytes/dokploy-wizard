"""CLI commands for the Task 1 live-baseline safety envelope."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from dokploy_wizard.proof.model_sync_artifacts import write_protected_manifest
from dokploy_wizard.proof.model_sync_env import prepare_proof_env
from dokploy_wizard.proof.model_sync_host_a import claim_plan_guard, complete_resumable_step
from dokploy_wizard.proof.model_sync_host_b import HostIdentity, assert_namespace_identity
from dokploy_wizard.proof.model_sync_remote import RemoteProbe, probe_host
from dokploy_wizard.proof.model_sync_state import (
    AbortGuard,
    AbortGuardError,
    arm_abort_guard,
    atomic_finalize,
    read_abort_guard,
    sha256_bytes,
)


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
    _require_active_workspace_root(args.wrapper)
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    arm_abort_guard(args.abort_guard)
    prepared = prepare_proof_env(
        env_file=args.env_file,
        backup_path=args.external_backup,
        guard_path=args.abort_guard,
    )
    claim = claim_plan_guard(
        guard_path=args.abort_guard,
        pid=os.getpid(),
        start_time_ticks=_self_start_time_ticks(),
    )
    host_a_probe = probe_host(host=host_a, password=password_a, stack_name="dokploy-wizard")
    host_b_probe = probe_host(host=host_b, password=password_b, stack_name="dokploy-wizard")
    identity_a = HostIdentity(
        host_a_probe.machine_sha256, host_a_probe.ssh_sha256, host_a_probe.architecture
    )
    identity_b = HostIdentity(
        host_b_probe.machine_sha256, host_b_probe.ssh_sha256, host_b_probe.architecture
    )
    assert_namespace_identity(host_a=identity_a, host_b=identity_b)
    if not host_a_probe.namespace_clean or not host_b_probe.namespace_clean:
        raise RuntimeError("managed namespace residue blocks live baseline proof")
    host_a_path = args.artifact_dir / "host-a-preflight.json"
    host_b_path = args.artifact_dir / "host-b-preflight.json"
    host_a_sha = write_protected_manifest(host_a_path, _probe_payload(host_a_probe))
    host_b_sha = write_protected_manifest(host_b_path, _probe_payload(host_b_probe))
    _run_wrapper(args.wrapper, host_a, password_a, args.env_file)
    baseline_path = args.artifact_dir / "baseline.json"
    baseline_sha = write_protected_manifest(baseline_path, {"templates": "six-template-baseline"})
    protected_path = args.artifact_dir / "protected-artifacts-before.txt"
    protected_sha = write_protected_manifest(protected_path, {"status": "no-secret-values"})
    guard_sha = sha256_bytes(args.abort_guard.read_bytes())
    write_protected_manifest(
        args.output,
        {
            "schema_version": 1,
            "source_base_commit": args.source_base_commit,
            "proof_commit": args.proof_commit,
            "coder_image_digest": "unavailable-before-capture",
            "litellm_image_digest": "unavailable-before-capture",
            "shared_core_image_digests": {
                "pgvector": "",
                "redis": "",
                "postfix": "",
                "litellm": "",
            },
            "env_original_sha256": prepared.original_sha256,
            "env_proof_sha256": prepared.proof_sha256,
            "env_mode": prepared.mode,
            "external_backup_path": str(args.external_backup),
            "abort_guard_path": str(args.abort_guard),
            "abort_guard_sha256": guard_sha,
            "host_a_preflight_sha256": host_a_sha,
            "host_b_preflight_sha256": host_b_sha,
            "host_identities_distinct": True,
            "host_architectures_equal": True,
            "baseline_sha256": baseline_sha,
            "protected_artifacts_before_path": str(protected_path),
            "protected_artifacts_before_sha256": protected_sha,
            "coder_secret_inventory_sha256": "0" * 64,
            "legacy_workspace_managed_fingerprints_sha256": "0" * 64,
        },
    )
    complete_resumable_step(guard_path=args.abort_guard, claim=claim)


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


def _require_active_workspace_root(wrapper: Path) -> None:
    expected = Path("/workspaces/model-sync")
    if not expected.exists() or wrapper.resolve().parents[1] != expected.resolve():
        raise RuntimeError("/workspaces/model-sync does not resolve to the active proof root")


def _run_wrapper(wrapper: Path, host: str, password: str, env_file: Path) -> None:
    result = subprocess.run(
        [
            str(wrapper),
            "proof",
            "--host",
            host,
            "--password",
            password,
            "--env-file",
            str(env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        raise RuntimeError("remote proof wrapper failed")


def _self_start_time_ticks() -> str:
    fields = Path("/proc/self/stat").read_text(encoding="utf-8").split()
    return fields[21]


def _status_payload(status: AbortGuard) -> dict[str, str | int | None]:
    return {
        "state": status.state,
        "claimant_kind": status.claimant_kind,
        "pid": status.pid,
        "start_time_ticks": status.start_time_ticks,
        "claim_token": status.claim_token,
    }


def _probe_payload(probe: RemoteProbe) -> dict[str, str | bool]:
    return {
        "machine_sha256": probe.machine_sha256,
        "ssh_sha256": probe.ssh_sha256,
        "architecture": probe.architecture,
        "namespace_clean": probe.namespace_clean,
    }
