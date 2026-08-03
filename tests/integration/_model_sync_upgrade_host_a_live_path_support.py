from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_lifecycle_schema import SingleHostLifecycleReceipt
from dokploy_wizard.proof.model_sync_results import ProofTransport
from dokploy_wizard.proof.model_sync_upgrade_host_a_observations import (
    CatalogObservation,
    HostAObservation,
    MigrationObservation,
    ReleaseObservation,
    ScheduleObservation,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_process import ModifyCommandObservation
from dokploy_wizard.proof.model_sync_upgrade_host_a_production import ProductionUpgradeConfig
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostABinding
from dokploy_wizard.remote_transport import (
    RemoteCommandCaptureLimits,
    RemoteCommandFailure,
    RemoteCommandOutput,
)

BLOCKED_CODE = "CODER_RETIRED_WORKSPACE_NOT_STOPPED"
FINAL_COMMIT = "a" * 40


@dataclass(frozen=True, slots=True)
class ModifyExecutionFixture:
    command: ModifyCommandObservation
    before: HostAObservation
    after: HostAObservation


class SequencedTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.events: list[str] = []
        self.commands: dict[str, str] = {}

    def ensure_dir(self, _remote_path: str) -> None: ...

    def upload(self, _local_path: Path, _remote_path: str) -> None: ...

    def chmod(self, _remote_path: str, _mode: int) -> None: ...

    def run(self, subcommand: str, command: str) -> None:
        self.events.append(subcommand)
        self.commands[subcommand] = command
        if subcommand == "modify":
            raise RemoteCommandFailure(
                subcommand=subcommand,
                error=RuntimeError(BLOCKED_CODE),
            )

    def capture(
        self,
        subcommand: str,
        command: str,
        _limits: RemoteCommandCaptureLimits,
    ) -> RemoteCommandOutput:
        if not self.events or self.events[0] != "activate-release":
            raise AssertionError("observation collector ran before final release activation")
        self.events.append(subcommand)
        self.commands[subcommand] = command
        return RemoteCommandOutput(self.payload, b"")

    def close(self) -> None:
        self.events.append("close")


def production_config(tmp_path: Path) -> ProductionUpgradeConfig:
    lifecycle = SingleHostLifecycleReceipt(
        phase="baseline_epoch",
        machine_sha256="1" * 64,
        ssh_sha256="2" * 64,
        architecture="amd64",
        baseline_boot_sha256="3" * 64,
        current_boot_sha256="3" * 64,
        baseline_epoch_id="4" * 64,
        host_a_epoch_id=None,
        teardown_epoch_id=None,
        final_epoch_id=None,
        previous_receipt_sha256=None,
        evidence_sha256="5" * 64,
        namespace_resource_absence_verified=True,
        fresh_install_epoch_verified=False,
        temporal_clean_epoch_evidence=False,
    )
    return ProductionUpgradeConfig(
        "fixture-host",
        "fixture-password",
        tmp_path / "dokploy-wizard-remote",
        tmp_path / "install.env",
        tmp_path,
        UpgradeHostABinding(
            "6" * 64,
            "7" * 64,
            "8" * 64,
            lifecycle,
            "9" * 64,
            0o600,
            FINAL_COMMIT,
            proof.ProofNamespace("stack", (), (), (), (), ()),
        ),
        proof.ProofNamespace("stack", (), (), (), (), ()),
        ProofTransport(None, None, "example.test", None, None, None, None, None, None, False),
    )


def observation(commit: str = FINAL_COMMIT) -> HostAObservation:
    return HostAObservation(
        ReleaseObservation(
            commit,
            "b" * 64,
            "c" * 64,
            Path(f"/root/dokploy-wizard/releases/{'b' * 64}"),
        ),
        MigrationObservation(
            "completed",
            "d" * 64,
            (),
            "ubuntu-vscode-opencode-pi",
            (
                "ubuntu-vscode-hermes",
                "ubuntu-vscode-kdense-byok",
                "ubuntu-vscode-opencode-pi",
                "ubuntu-vscode-opencode-web",
            ),
            ("ubuntu-vscode-openwork", "ubuntu-vscode-pi-web"),
        ),
        CatalogObservation(
            "e" * 64,
            Path("/var/lib/docker/volumes/catalog/_data/generation.json"),
            "e" * 64,
            "e" * 64,
            Path("/var/lib/docker/volumes/catalog/_data/state.json"),
        ),
        ScheduleObservation("f" * 64, "f" * 64, "0" * 64, "0" * 64, "1" * 64),
        (),
    )


def observation_payload(commit: str = FINAL_COMMIT) -> dict[str, proof.JsonValue]:
    observed = observation(commit)
    assert observed.migration is not None
    assert observed.catalog is not None
    assert observed.schedule is not None
    return {
        "catalog": {
            "applied_model_set_sha256": observed.catalog.applied_model_set_sha256,
            "generation_mode": 0o600,
            "generation_path": str(observed.catalog.generation_path),
            "generation_sha256": observed.catalog.generation_sha256,
            "state_mode": 0o600,
            "state_output_sha256": observed.catalog.state_output_sha256,
            "state_path": str(observed.catalog.state_path),
        },
        "migration": {
            "mode": 0o600,
            "mutation_events": [],
            "path": (
                "/root/dokploy-wizard/state/coder-template-migration/"
                "coder-migration-receipts-v1.json"
            ),
            "primary_name": observed.migration.primary_name,
            "push_names": list(observed.migration.push_names),
            "receipt_sha256": observed.migration.receipt_sha256,
            "retired_names": list(observed.migration.retired_names),
            "status": observed.migration.status,
        },
        "release": {
            "active_release_path": str(observed.release.active_release_path),
            "archive_sha256": observed.release.archive_sha256,
            "commit_sha": observed.release.commit_sha,
            "manifest_mode": 0o644,
            "manifest_path": "/root/dokploy-wizard/release-manifest.json",
            "manifest_sha256": observed.release.manifest_sha256,
        },
        "schedule": {
            "applied_desired_sha256": observed.schedule.applied_desired_sha256,
            "desired_sha256": observed.schedule.desired_sha256,
            "desired_spec_sha256": observed.schedule.desired_spec_sha256,
            "receipt_mode": 0o600,
            "receipt_path": (
                "/root/dokploy-wizard/state/shared-core-sync/schedule-receipts/"
                f"{observed.schedule.receipt_sha256}.json"
            ),
            "receipt_sha256": observed.schedule.receipt_sha256,
            "remote_spec_sha256": observed.schedule.remote_spec_sha256,
        },
        "schema_version": 1,
        "sync_results": [],
    }


def write_wrapper(path: Path) -> None:
    payload = json.dumps(observation_payload(), sort_keys=True, separators=(",", ":"))
    summary = json.dumps(
        {"lifecycle": {"mode": "modify", "phases_to_run": ["shared_core", "coder"]}}
    )
    before_line = f"[remote:modify-observation-before:stdout] {payload}"
    summary_line = f"[remote:modify:stdout] {summary}"
    after_line = f"[remote:modify-observation-after:stdout] {payload}"
    source = f"""#!/usr/bin/env python3
import sys
required = {{"--verbose", "--capture-upgrade-observations"}}
if not required <= set(sys.argv[1:]):
    raise SystemExit(9)
print({before_line!r}, file=sys.stderr)
print({summary_line!r}, file=sys.stderr)
print({after_line!r}, file=sys.stderr)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
