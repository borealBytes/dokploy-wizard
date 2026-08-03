"""Fail-closed mutation inventory and strict-rerun accounting contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar, assert_never

Result = TypeVar("Result")


class MutationBackend(StrEnum):
    """Every provider or control-plane surface permitted during a proof."""

    DOKPLOY = "dokploy"
    DOCKER = "docker"
    CLOUDFLARE = "cloudflare"
    TAILSCALE = "tailscale"
    SHELL_PROCESS = "shell_process"
    CODER = "coder"
    LITELLM_DB_ADMIN = "litellm_db_admin"
    SCHEDULE_HELPER = "schedule_helper"
    WORKSPACE_PROOF = "workspace_proof"


class MutationKind(StrEnum):
    """Counters that strict idempotency is required to keep at zero."""

    CONTROL_PLANE = "control_plane"
    SYNCHRONIZER_DURABLE_WRITE = "synchronizer_durable_write"
    WORKSPACE_PROOF = "workspace_proof"


@dataclass(frozen=True, slots=True)
class StrictMutationTotals:
    """Typed, value-free strict-pass mutation totals."""

    control_plane_mutations: int = 0
    synchronizer_durable_writes: int = 0
    unregistered_mutators: int = 0

    def __post_init__(self) -> None:
        if min(
            self.control_plane_mutations,
            self.synchronizer_durable_writes,
            self.unregistered_mutators,
        ) < 0:
            raise MutationRegistryError("mutation totals cannot be negative")

    def record(self, kind: MutationKind) -> StrictMutationTotals:
        match kind:
            case MutationKind.CONTROL_PLANE:
                return StrictMutationTotals(
                    control_plane_mutations=self.control_plane_mutations + 1,
                    synchronizer_durable_writes=self.synchronizer_durable_writes,
                    unregistered_mutators=self.unregistered_mutators,
                )
            case MutationKind.SYNCHRONIZER_DURABLE_WRITE:
                return StrictMutationTotals(
                    control_plane_mutations=self.control_plane_mutations,
                    synchronizer_durable_writes=self.synchronizer_durable_writes + 1,
                    unregistered_mutators=self.unregistered_mutators,
                )
            case MutationKind.WORKSPACE_PROOF:
                return self
            case unexpected:
                assert_never(unexpected)

    def record_unregistered(self) -> StrictMutationTotals:
        return StrictMutationTotals(
            control_plane_mutations=self.control_plane_mutations,
            synchronizer_durable_writes=self.synchronizer_durable_writes,
            unregistered_mutators=self.unregistered_mutators + 1,
        )


class MutationRegistryError(RuntimeError):
    """Base error for proof mutation authorization failures."""


class UnregisteredMutatorError(MutationRegistryError):
    """Carries the incremented total while withholding an unknown adapter name."""

    def __init__(self, totals: StrictMutationTotals) -> None:
        super().__init__()
        self.totals = totals

    def __str__(self) -> str:
        return "proof mutation backend is not registered"


class StrictLeaseNotHeldError(MutationRegistryError):
    """Raised before a strict pass can dispatch any helper work."""

    def __str__(self) -> str:
        return "strict proof lease is not held"


class StrictMutationError(MutationRegistryError):
    """Carries typed strict-pass totals that violate idempotency."""

    def __init__(self, totals: StrictMutationTotals) -> None:
        super().__init__()
        self.totals = totals

    def __str__(self) -> str:
        return "strict proof observed nonzero mutation totals"


@dataclass(frozen=True, slots=True)
class MutationInventory:
    """Closed inventory that rejects omission of any mutation backend."""

    backends: frozenset[MutationBackend]

    def __post_init__(self) -> None:
        if self.backends != frozenset(MutationBackend):
            raise MutationRegistryError("proof mutation inventory is incomplete")

    @classmethod
    def required(cls) -> MutationInventory:
        return cls(frozenset(MutationBackend))


@dataclass(frozen=True, slots=True)
class MutationAuthorization:
    """Private-to-the-contract proof that one known backend may mutate."""

    backend: MutationBackend
    kind: MutationKind


@dataclass(slots=True)
class _StrictLeaseCapability:
    active: bool = True


@dataclass(slots=True)
class ProofMutationRecorder:
    """Mutable proof-scoped accounting that authorizes every external mutation first."""

    inventory: MutationInventory
    _lease: _StrictLeaseCapability
    _totals: StrictMutationTotals = field(default_factory=StrictMutationTotals)

    @property
    def totals(self) -> StrictMutationTotals:
        """Return the current value-free strict-pass totals."""

        return self._totals

    def execute(self, *, backend: str, kind: MutationKind, action: Callable[[], None]) -> None:
        """Authorize and execute one mutation before recording its completed effect."""

        self.execute_result(backend=backend, kind=kind, action=action)

    def execute_result(
        self,
        *,
        backend: str,
        kind: MutationKind,
        action: Callable[[], Result],
    ) -> Result:
        """Authorize a mutation and return the completed provider result."""

        if not self._lease.active:
            raise StrictLeaseNotHeldError()
        authorization = authorize_mutation(
            inventory=self.inventory,
            totals=self._totals,
            backend=backend,
            kind=kind,
        )
        result = action()
        self._totals = self._totals.record(authorization.kind)
        return result

    def assert_strict_zero(self) -> None:
        """Fail closed unless the strict lease is held and totals remain zero."""

        assert_strict_totals(self._totals, lease=self._lease)


def authorize_mutation(
    *,
    inventory: MutationInventory,
    totals: StrictMutationTotals,
    backend: str,
    kind: MutationKind,
) -> MutationAuthorization:
    """Authorize a known backend before the caller invokes its external action."""

    try:
        parsed_backend = MutationBackend(backend)
    except ValueError as error:
        raise UnregisteredMutatorError(totals.record_unregistered()) from error
    if parsed_backend not in inventory.backends:
        raise UnregisteredMutatorError(totals.record_unregistered())
    return MutationAuthorization(parsed_backend, kind)


def execute_registered_mutation(
    *,
    inventory: MutationInventory,
    totals: StrictMutationTotals,
    backend: str,
    kind: MutationKind,
    action: Callable[[], None],
) -> StrictMutationTotals:
    """Reject unknown mutation adapters before invoking their external operation."""

    authorization = authorize_mutation(
        inventory=inventory,
        totals=totals,
        backend=backend,
        kind=kind,
    )
    action()
    return totals.record(authorization.kind)


def assert_strict_totals(totals: StrictMutationTotals, *, lease: _StrictLeaseCapability) -> None:
    """Require strict lease ownership and zero strict-only counters."""

    if not lease.active:
        raise StrictLeaseNotHeldError()
    if (
        totals.control_plane_mutations != 0
        or totals.synchronizer_durable_writes != 0
        or totals.unregistered_mutators != 0
    ):
        raise StrictMutationError(totals)
