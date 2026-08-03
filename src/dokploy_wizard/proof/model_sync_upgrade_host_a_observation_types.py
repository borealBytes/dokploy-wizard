"""Typed values derived from Host A's authoritative remote artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeAlias

from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError

RETAINED_NAMES = frozenset(
    (
        "ubuntu-vscode-hermes",
        "ubuntu-vscode-kdense-byok",
        "ubuntu-vscode-opencode-pi",
        "ubuntu-vscode-opencode-web",
    )
)
RETIRED_NAMES = frozenset(("ubuntu-vscode-openwork", "ubuntu-vscode-pi-web"))
MigrationStatus: TypeAlias = Literal["planned", "running", "blocked", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class CatalogObservation:
    applied_model_set_sha256: str
    generation_path: Path
    generation_sha256: str
    state_output_sha256: str
    state_path: Path

    @property
    def exact(self) -> bool:
        return self.applied_model_set_sha256 == self.generation_sha256 == self.state_output_sha256


@dataclass(frozen=True, slots=True)
class MigrationObservation:
    status: MigrationStatus
    receipt_sha256: str
    mutation_events: tuple[str, ...]
    primary_name: str
    push_names: tuple[str, ...]
    retired_names: tuple[str, ...]

    @property
    def source_exact(self) -> bool:
        return (
            self.status == "completed"
            and self.primary_name == "ubuntu-vscode-opencode-pi"
            and frozenset(self.push_names) == RETAINED_NAMES
            and frozenset(self.retired_names) == RETIRED_NAMES
        )


@dataclass(frozen=True, slots=True)
class ReleaseObservation:
    commit_sha: str
    archive_sha256: str
    manifest_sha256: str
    active_release_path: Path


@dataclass(frozen=True, slots=True)
class ScheduleObservation:
    desired_sha256: str
    applied_desired_sha256: str
    desired_spec_sha256: str
    remote_spec_sha256: str
    receipt_sha256: str

    @property
    def exact(self) -> bool:
        return (
            self.desired_sha256 == self.applied_desired_sha256
            and self.desired_spec_sha256 == self.remote_spec_sha256
        )


@dataclass(frozen=True, slots=True)
class SyncResultObservation:
    result_sha256: str
    durable_write_count: int
    path: Path


@dataclass(frozen=True, slots=True)
class HostAObservation:
    release: ReleaseObservation
    migration: MigrationObservation | None
    catalog: CatalogObservation | None
    schedule: ScheduleObservation | None
    sync_results: tuple[SyncResultObservation, ...]

    @property
    def catalog_exact(self) -> bool:
        return self.catalog is not None and self.catalog.exact

    @property
    def schedule_exact(self) -> bool:
        return self.schedule is not None and self.schedule.exact

    @property
    def source_exact(self) -> bool:
        return self.migration is not None and self.migration.source_exact


class HostAObserver(Protocol):
    def capture(self) -> HostAObservation: ...


@dataclass(frozen=True, slots=True)
class ObservedMutationTotals:
    control_plane_mutations: int
    synchronizer_durable_writes: int


def derive_observed_totals(
    before: HostAObservation, after: HostAObservation
) -> ObservedMutationTotals:
    """Derive deltas only from newly durable mutation and helper result receipts."""

    before_mutations = _mutation_events(before)
    after_mutations = _mutation_events(after)
    before_sync = {item.result_sha256: item for item in before.sync_results}
    after_sync = {item.result_sha256: item for item in after.sync_results}
    mutation_events_preserved = before_mutations.issubset(after_mutations)
    sync_results_preserved = before_sync.keys() <= after_sync.keys()
    if not mutation_events_preserved or not sync_results_preserved:
        raise UpgradeHostAError("Host A authoritative observation regressed")
    if any(after_sync[digest] != item for digest, item in before_sync.items()):
        raise UpgradeHostAError("Host A synchronizer result observation regressed")
    return ObservedMutationTotals(
        control_plane_mutations=len(after_mutations - before_mutations),
        synchronizer_durable_writes=sum(
            item.durable_write_count
            for digest, item in after_sync.items()
            if digest not in before_sync
        ),
    )


def _mutation_events(observation: HostAObservation) -> frozenset[str]:
    if observation.migration is None:
        return frozenset()
    return frozenset(observation.migration.mutation_events)
