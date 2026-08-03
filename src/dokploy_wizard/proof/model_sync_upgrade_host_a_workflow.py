"""Blocked-first Host A upgrade orchestration and evidence publication."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_artifacts import write_or_verify_exact_bytes
from dokploy_wizard.proof.model_sync_identity import RemoteProbe
from dokploy_wizard.proof.model_sync_lifecycle import (
    HostLifecycleEpoch,
    publish_lifecycle_receipt,
    receipt_sha256,
    record_host_a_epoch,
)
from dokploy_wizard.proof.model_sync_strict_proof import StrictProofResult
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    ManagedHostSnapshot,
    ModifyAttempt,
    RetiredFixtureEvidence,
    UpgradeHostAError,
    UpgradeHostAExecution,
    UpgradeHostAOperations,
)

_BLOCKED_CODE = "CODER_RETIRED_WORKSPACE_NOT_STOPPED"
_RETAINED_TEMPLATES = (
    "ubuntu-vscode-hermes",
    "ubuntu-vscode-kdense-byok",
    "ubuntu-vscode-opencode-pi",
    "ubuntu-vscode-opencode-web",
)


@dataclass(frozen=True, slots=True)
class _UpgradeObservations:
    fixtures: RetiredFixtureEvidence
    before_probe: RemoteProbe
    after_probe: RemoteProbe
    before_snapshot: ManagedHostSnapshot
    after_snapshot: ManagedHostSnapshot
    blocked: ModifyAttempt
    upgraded: ModifyAttempt
    strict: StrictProofResult


def execute_upgrade_host_a(
    execution: UpgradeHostAExecution,
    operations: UpgradeHostAOperations,
) -> None:
    """Run the exact blocked, resumed, and strict Host A upgrade sequence."""

    before_probe = operations.probe()
    _require_host_continuity(execution, before_probe)
    before_snapshot = operations.snapshot()
    fixtures = operations.create_retired_fixtures()
    destructive_before = operations.destructive_state_sha256()
    blocked = operations.modify()
    destructive_after = operations.destructive_state_sha256()
    _require_blocked(blocked, destructive_before, destructive_after)
    operations.stop_running_fixture(fixtures.running_workspace_id)
    upgraded = operations.modify()
    _require_upgrade(execution, upgraded)
    after_snapshot = operations.snapshot()
    _require_snapshot_continuity(before_snapshot, after_snapshot)
    strict = operations.strict_proof()
    _require_strict(strict)
    after_probe = operations.probe()
    _require_host_continuity(execution, after_probe)
    if _identity(before_probe) != _identity(after_probe):
        raise UpgradeHostAError("Host A identity changed during upgrade")
    epoch = HostLifecycleEpoch(
        epoch_id=_epoch_id(execution, after_probe),
        expected_previous_sha256=execution.binding.lifecycle_sha256,
    )
    lifecycle = record_host_a_epoch(execution.binding.lifecycle, after_probe, epoch)
    publish_lifecycle_receipt(execution.lifecycle_output_path, lifecycle)
    lifecycle_output_sha256 = receipt_sha256(lifecycle)
    write_or_verify_exact_bytes(
        execution.result_output_path,
        proof.canonical_json_bytes(
            _result_payload(
                execution,
                _UpgradeObservations(
                    fixtures,
                    before_probe,
                    after_probe,
                    before_snapshot,
                    after_snapshot,
                    blocked,
                    upgraded,
                    strict,
                ),
                lifecycle_output_sha256,
            )
        )
        + b"\n",
    )


def _require_host_continuity(execution: UpgradeHostAExecution, probe: RemoteProbe) -> None:
    lifecycle = execution.binding.lifecycle
    observed = (probe.machine_sha256, probe.ssh_sha256, probe.architecture, probe.boot_sha256)
    expected = (
        lifecycle.machine_sha256,
        lifecycle.ssh_sha256,
        lifecycle.architecture,
        lifecycle.baseline_boot_sha256,
    )
    if observed != expected or probe.namespace_clean:
        raise UpgradeHostAError("Host A managed identity does not match the Task 1 baseline")


def _require_blocked(blocked: ModifyAttempt, before: str, after: str) -> None:
    if (
        blocked.exit_code != 1
        or blocked.failure_code != _BLOCKED_CODE
        or blocked.deployed_commit is not None
        or blocked.control_plane_mutations != 0
        or blocked.synchronizer_durable_writes != 0
        or before != after
    ):
        raise UpgradeHostAError("First Host A modify did not fail closed on the running fixture")


def _require_upgrade(execution: UpgradeHostAExecution, upgraded: ModifyAttempt) -> None:
    if (
        upgraded.exit_code != 0
        or upgraded.failure_code is not None
        or upgraded.deployed_commit != execution.binding.final_commit
        or upgraded.control_plane_mutations <= 0
        or upgraded.synchronizer_durable_writes < 0
    ):
        raise UpgradeHostAError("Resumed Host A modify did not produce an exact upgrade")


def _require_snapshot_continuity(
    before: ManagedHostSnapshot,
    after: ManagedHostSnapshot,
) -> None:
    if before.primary_uuid != after.primary_uuid:
        raise UpgradeHostAError("Primary Coder template UUID changed during upgrade")
    if tuple(sorted(after.template_names)) != _RETAINED_TEMPLATES:
        raise UpgradeHostAError("Retained Coder template names are not exact")
    if not after.catalog_exact or not after.schedule_exact or not after.source_exact:
        raise UpgradeHostAError("Host A managed post-upgrade state is incomplete")


def _require_strict(strict: StrictProofResult) -> None:
    totals = strict.totals
    if (
        tuple(sorted(strict.template_names)) != _RETAINED_TEMPLATES
        or not strict.cleanup_complete
        or totals.control_plane_mutations != 0
        or totals.synchronizer_durable_writes != 0
        or totals.unregistered_mutators != 0
    ):
        raise UpgradeHostAError("Host A strict proof did not remain mutation-free")


def _identity(probe: RemoteProbe) -> dict[str, proof.JsonValue]:
    return {
        "architecture": probe.architecture,
        "boot_sha256": probe.boot_sha256,
        "machine_sha256": probe.machine_sha256,
        "ssh_sha256": probe.ssh_sha256,
    }


def _epoch_id(execution: UpgradeHostAExecution, probe: RemoteProbe) -> str:
    material = (
        b"host-a-epoch\0"
        + execution.binding.lifecycle.to_bytes()
        + execution.binding.final_commit.encode("ascii")
        + proof.canonical_json_bytes(_identity(probe))
    )
    return hashlib.sha256(material).hexdigest()


def _result_payload(
    execution: UpgradeHostAExecution,
    observed: _UpgradeObservations,
    lifecycle_output_sha256: str,
) -> dict[str, proof.JsonValue]:
    strict_totals = observed.strict.totals
    return {
        "baseline_result_sha256": execution.binding.baseline_result_sha256,
        "baseline_sha256": execution.binding.baseline_sha256,
        "blocked_destructive_state_equal": True,
        "blocked_failure_code": observed.blocked.failure_code,
        "blocked_modify_exit": observed.blocked.exit_code,
        "catalog_exact": observed.after_snapshot.catalog_exact,
        "deployed_commit": observed.upgraded.deployed_commit,
        "deployed_commit_matches": (
            observed.upgraded.deployed_commit == execution.binding.final_commit
        ),
        "final_commit": execution.binding.final_commit,
        "host_identities_distinct": False,
        "host_identity_mode": "single_sequential",
        "identity_after": _identity(observed.after_probe),
        "identity_before": _identity(observed.before_probe),
        "lifecycle_input_path": str(execution.lifecycle_input_path.resolve()),
        "lifecycle_input_sha256": execution.binding.lifecycle_sha256,
        "lifecycle_output_path": str(execution.lifecycle_output_path.resolve()),
        "lifecycle_output_sha256": lifecycle_output_sha256,
        "primary_uuid_after": observed.after_snapshot.primary_uuid,
        "primary_uuid_before": observed.before_snapshot.primary_uuid,
        "proof_workspace_cleanup_complete": observed.strict.cleanup_complete,
        "retained_state_after_sha256": observed.after_snapshot.retained_state_sha256,
        "retained_state_before_sha256": observed.before_snapshot.retained_state_sha256,
        "running_fixture": _fixture_payload(observed.fixtures, running=True),
        "schedule_exact": observed.after_snapshot.schedule_exact,
        "schema_version": 1,
        "source_exact": observed.after_snapshot.source_exact,
        "stopped_fixture": _fixture_payload(observed.fixtures, running=False),
        "strict_pass_control_plane_mutations_total": strict_totals.control_plane_mutations,
        "strict_pass_synchronizer_durable_writes_total": strict_totals.synchronizer_durable_writes,
        "strict_pass_unregistered_mutators_total": strict_totals.unregistered_mutators,
        "template_names": list(_RETAINED_TEMPLATES),
        "temporal_clean_epoch_evidence": False,
        "upgrade_control_plane_mutations_total": observed.upgraded.control_plane_mutations,
        "upgrade_synchronizer_durable_writes_total": observed.upgraded.synchronizer_durable_writes,
    }


def _fixture_payload(
    fixtures: RetiredFixtureEvidence,
    *,
    running: bool,
) -> dict[str, proof.JsonValue]:
    if running:
        return {
            "template_id": fixtures.running_template_id,
            "template_name": fixtures.running_template_name,
            "workspace_id": fixtures.running_workspace_id,
            "workspace_name": fixtures.running_workspace_name,
        }
    return {
        "template_id": fixtures.stopped_template_id,
        "template_name": fixtures.stopped_template_name,
        "workspace_id": fixtures.stopped_workspace_id,
        "workspace_name": fixtures.stopped_workspace_name,
    }
