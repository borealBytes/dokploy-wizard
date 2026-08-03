"""Typed inputs and observations for the Host A upgrade proof."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dokploy_wizard.proof import ProofNamespace
from dokploy_wizard.proof.model_sync_identity import RemoteProbe
from dokploy_wizard.proof.model_sync_lifecycle_schema import SingleHostLifecycleReceipt
from dokploy_wizard.proof.model_sync_strict_proof import StrictProofResult


@dataclass(frozen=True, slots=True)
class UpgradeHostABinding:
    """Immutable Task 1 evidence accepted by the Host A upgrade command."""

    baseline_sha256: str
    baseline_result_sha256: str
    lifecycle_sha256: str
    lifecycle: SingleHostLifecycleReceipt
    env_sha256: str
    env_mode: int
    final_commit: str
    namespace: ProofNamespace


class UpgradeHostAError(RuntimeError):
    """Raised before mutation when Task 18 evidence or observations are unsafe."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class UpgradeHostAExecution:
    binding: UpgradeHostABinding
    lifecycle_input_path: Path
    lifecycle_output_path: Path
    result_output_path: Path


@dataclass(frozen=True, slots=True)
class ManagedHostSnapshot:
    primary_uuid: str
    template_names: tuple[str, ...]
    retained_state_sha256: str
    catalog_exact: bool
    schedule_exact: bool
    source_exact: bool


@dataclass(frozen=True, slots=True)
class RetiredFixtureEvidence:
    running_workspace_id: str
    running_workspace_name: str
    running_template_id: str
    running_template_name: str
    stopped_workspace_id: str
    stopped_workspace_name: str
    stopped_template_id: str
    stopped_template_name: str


@dataclass(frozen=True, slots=True)
class ModifyAttempt:
    exit_code: int
    failure_code: str | None
    deployed_commit: str | None
    control_plane_mutations: int
    synchronizer_durable_writes: int


class UpgradeHostAOperations(Protocol):
    def probe(self) -> RemoteProbe: ...

    def snapshot(self) -> ManagedHostSnapshot: ...

    def create_retired_fixtures(self) -> RetiredFixtureEvidence: ...

    def destructive_state_sha256(self) -> str: ...

    def modify(self) -> ModifyAttempt: ...

    def stop_running_fixture(self, workspace_id: str) -> None: ...

    def strict_proof(self) -> StrictProofResult: ...


__all__ = (
    "ManagedHostSnapshot",
    "ModifyAttempt",
    "RetiredFixtureEvidence",
    "UpgradeHostABinding",
    "UpgradeHostAError",
    "UpgradeHostAExecution",
    "UpgradeHostAOperations",
)
