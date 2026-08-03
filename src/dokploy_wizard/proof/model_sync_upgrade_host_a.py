"""Fail-closed Task 1 binding for the Host A upgrade proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_artifacts import JsonValue, require_mapping, require_sha256
from dokploy_wizard.proof.model_sync_lifecycle import receipt_sha256
from dokploy_wizard.proof.model_sync_lifecycle_schema import (
    SingleHostLifecycleReceipt as SingleHostLifecycleReceipt,
)
from dokploy_wizard.proof.model_sync_lifecycle_schema import (
    parse_lifecycle_receipt,
)
from dokploy_wizard.proof.model_sync_task1_bound_namespace import (
    resolve_bound_task1_namespace,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_production_factory import (
    build_production_operations,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    ManagedHostSnapshot as ManagedHostSnapshot,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    ModifyAttempt as ModifyAttempt,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    RetiredFixtureEvidence as RetiredFixtureEvidence,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    UpgradeHostABinding as UpgradeHostABinding,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    UpgradeHostAError as UpgradeHostAError,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    UpgradeHostAExecution as UpgradeHostAExecution,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_workflow import (
    execute_upgrade_host_a as execute_upgrade_host_a,
)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_JSON_BYTES = 16 * 1024 * 1024


def run_upgrade_host_a(args: argparse.Namespace) -> None:
    """Validate every Task 1 binding before the live upgrade orchestration starts."""

    binding = load_upgrade_binding(args)
    execution = UpgradeHostAExecution(
        binding=binding,
        lifecycle_input_path=args.lifecycle_input,
        lifecycle_output_path=args.lifecycle_output,
        result_output_path=args.output,
    )
    execute_upgrade_host_a(execution, build_production_operations(args, binding))


def load_upgrade_binding(args: argparse.Namespace) -> UpgradeHostABinding:
    """Parse and cross-bind immutable Task 1 artifacts without external callbacks."""

    if not args.single_host_sequential:
        raise UpgradeHostAError("Host A upgrade requires single-host sequential mode")
    if _COMMIT.fullmatch(args.final_commit) is None:
        raise UpgradeHostAError("Host A upgrade final commit is invalid")
    baseline_bytes = _read(args.baseline, "Task 1 baseline")
    result_bytes = _read(args.baseline_result, "Task 1 result")
    baseline = require_mapping(_json(baseline_bytes, "Task 1 baseline"), "Task 1 baseline")
    result = require_mapping(_json(result_bytes, "Task 1 result"), "Task 1 result")
    baseline_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    if result.get("baseline_sha256") != baseline_sha256:
        raise UpgradeHostAError("Task 1 baseline hash does not match its result")
    if result.get("host_identity_mode") != "single_sequential":
        raise UpgradeHostAError("Task 1 result is not single-host sequential")
    lifecycle_bytes = _read(args.lifecycle_input, "Task 1 lifecycle receipt")
    lifecycle_sha256 = hashlib.sha256(lifecycle_bytes).hexdigest()
    if result.get("single_host_lifecycle_sha256") != lifecycle_sha256:
        raise UpgradeHostAError("Task 1 lifecycle hash does not match its result")
    lifecycle = parse_lifecycle_receipt(_json(lifecycle_bytes, "Task 1 lifecycle receipt"))
    if lifecycle.phase != "baseline_epoch" or receipt_sha256(lifecycle) != lifecycle_sha256:
        raise UpgradeHostAError("Task 1 lifecycle receipt is not the baseline epoch")
    guard_bytes = _read(args.abort_guard, "Task 1 abort guard")
    guard = proof.parse_abort_guard(_json(guard_bytes, "Task 1 abort guard"))
    if guard.phase != "complete" or guard.claimant_kind != "plan" or guard.attestation is None:
        raise UpgradeHostAError("Task 1 abort guard is not complete")
    if guard.env_receipt is None:
        raise UpgradeHostAError("Task 1 abort guard has no proof env receipt")
    attestation = guard.attestation
    artifact_dir = Path(attestation.artifact_dir)
    expected_paths = (
        (args.abort_guard.resolve(), Path(attestation.guard_path)),
        (args.baseline.resolve(), artifact_dir / "baseline.json"),
        (args.baseline_result.resolve(), Path(attestation.result_path)),
        (
            args.lifecycle_input.resolve(),
            artifact_dir / "single-host-lifecycle-baseline.json",
        ),
    )
    if any(observed != expected for observed, expected in expected_paths):
        raise UpgradeHostAError("Task 1 artifact paths do not match their attestation")
    proof.verify_result_bytes(guard.attestation, result_bytes)
    if result.get("abort_guard_path") != str(args.abort_guard.resolve()):
        raise UpgradeHostAError("Task 1 result does not bind the supplied abort guard")
    if result.get("single_host_lifecycle_path") != str(args.lifecycle_input.resolve()):
        raise UpgradeHostAError("Task 1 result does not bind the supplied lifecycle receipt")
    if result.get("host_a_preflight_sha256") != lifecycle.evidence_sha256:
        raise UpgradeHostAError("Task 1 lifecycle identity does not bind its preflight")
    try:
        proof.verify_attestation(
            proof.ProofRecoveryPaths(
                args.env_file,
                Path(guard.env_receipt.backup_path),
                args.abort_guard,
                artifact_dir,
                args.baseline_result,
                args.wrapper.resolve().parent.parent,
            ),
            guard,
            require_result=True,
        )
    except proof.AbortGuardError as error:
        raise UpgradeHostAError("Task 1 attestation is no longer valid") from error
    context_evidence = guard.attestation.context_evidence
    if context_evidence is None:
        raise UpgradeHostAError("Task 1 attestation has no proof namespace evidence")
    namespace = resolve_bound_task1_namespace(context_evidence)
    if baseline.get("stack_name") != namespace.stack_name:
        raise UpgradeHostAError("Task 1 baseline stack does not match its proof namespace")
    require_sha256(result.get("abort_guard_sha256"), "Task 1 abort guard")
    env_sha256 = require_sha256(result.get("env_proof_sha256"), "Task 1 proof env")
    env_mode = result.get("env_mode")
    if not isinstance(env_mode, int) or isinstance(env_mode, bool) or env_mode != 0o600:
        raise UpgradeHostAError("Task 1 result proof env mode is invalid")
    return UpgradeHostABinding(
        baseline_sha256=baseline_sha256,
        baseline_result_sha256=hashlib.sha256(result_bytes).hexdigest(),
        lifecycle_sha256=lifecycle_sha256,
        lifecycle=lifecycle,
        env_sha256=env_sha256,
        env_mode=env_mode,
        final_commit=args.final_commit,
        namespace=namespace,
    )


def _read(path: Path, label: str) -> bytes:
    try:
        return proof.read_proof_bytes(path, _MAX_JSON_BYTES, 0o600)
    except (OSError, proof.AbortGuardError) as error:
        raise UpgradeHostAError(f"{label} is absent or unsafe") from error


def _json(content: bytes, label: str) -> JsonValue:
    try:
        value: JsonValue = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpgradeHostAError(f"{label} is not valid JSON") from error
    return value


__all__ = (
    "ManagedHostSnapshot",
    "ModifyAttempt",
    "RetiredFixtureEvidence",
    "SingleHostLifecycleReceipt",
    "UpgradeHostABinding",
    "UpgradeHostAError",
    "UpgradeHostAExecution",
    "execute_upgrade_host_a",
    "load_upgrade_binding",
    "parse_lifecycle_receipt",
    "receipt_sha256",
    "run_upgrade_host_a",
)
