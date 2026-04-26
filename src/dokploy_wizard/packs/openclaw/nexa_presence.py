"""Explicit Nexa presence lifecycle state machine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

NexaLifecycleState = Literal["idle", "queued", "working", "refreshing", "failed", "done"]


@dataclass(frozen=True)
class NexaMappedStatus:
    """Optional user-visible status projection for supported environments."""

    status: str
    message: str | None = None


class NexaStatusAdapter(Protocol):
    """Capability-safe boundary for mapping internal state to visible status."""

    def map_state(self, state: NexaLifecycleState) -> NexaMappedStatus | None: ...


@dataclass(frozen=True)
class NexaPresenceSnapshot:
    """Current internal lifecycle state plus any safe external projection."""

    state: NexaLifecycleState
    failure_reason: str | None
    external_supported: bool
    external_status: str | None
    external_message: str | None


class NexaPresenceTransitionError(ValueError):
    """Raised when a lifecycle transition would be ambiguous or invalid."""


class NexaPresenceMachine:
    """Narrow state machine for Nexa runtime presence.

    This models internal lifecycle only. It intentionally does not encode queue
    ordering, fairness, cancellation, or any scheduling policy.
    """

    def __init__(
        self,
        *,
        initial_state: NexaLifecycleState = "idle",
        status_adapter: NexaStatusAdapter | None = None,
    ) -> None:
        self._state: NexaLifecycleState = initial_state
        self._failure_reason: str | None = None
        self._status_adapter = status_adapter

    @property
    def state(self) -> NexaLifecycleState:
        return self._state

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    def snapshot(self) -> NexaPresenceSnapshot:
        mapped = None
        if self._status_adapter is not None:
            mapped = self._status_adapter.map_state(self._state)
        return NexaPresenceSnapshot(
            state=self._state,
            failure_reason=self._failure_reason,
            external_supported=mapped is not None,
            external_status=None if mapped is None else mapped.status,
            external_message=None if mapped is None else mapped.message,
        )

    def enqueue(self) -> NexaPresenceSnapshot:
        return self._transition("queued", allowed_from=("idle", "failed", "done"))

    def start_work(self) -> NexaPresenceSnapshot:
        return self._transition("working", allowed_from=("queued",))

    def start_refresh(self) -> NexaPresenceSnapshot:
        return self._transition("refreshing", allowed_from=("working", "done"))

    def finish_refresh(self) -> NexaPresenceSnapshot:
        return self._transition("working", allowed_from=("refreshing",))

    def finish(self) -> NexaPresenceSnapshot:
        return self._transition("done", allowed_from=("working", "refreshing"))

    def fail(self, *, reason: str | None = None) -> NexaPresenceSnapshot:
        self._failure_reason = reason
        return self._transition("failed", allowed_from=("queued", "working", "refreshing"))

    def reset(self) -> NexaPresenceSnapshot:
        return self._transition("idle", allowed_from=("queued", "failed", "done"))

    def _transition(
        self,
        next_state: NexaLifecycleState,
        *,
        allowed_from: tuple[NexaLifecycleState, ...],
    ) -> NexaPresenceSnapshot:
        if self._state not in allowed_from:
            allowed = ", ".join(allowed_from)
            msg = (
                f"Cannot transition Nexa presence from '{self._state}' to '{next_state}'. "
                f"Allowed from: {allowed}."
            )
            raise NexaPresenceTransitionError(msg)

        self._state = next_state
        if next_state != "failed":
            self._failure_reason = None
        return self.snapshot()


class StaticNexaStatusAdapter:
    """Small test-friendly adapter for environments with simple status support."""

    def __init__(
        self,
        mapping: dict[NexaLifecycleState, NexaMappedStatus],
        *,
        supported: bool = True,
    ) -> None:
        self._mapping = mapping
        self._supported = supported

    def map_state(self, state: NexaLifecycleState) -> NexaMappedStatus | None:
        if not self._supported:
            return None
        return self._mapping.get(state)


__all__ = [
    "NexaLifecycleState",
    "NexaMappedStatus",
    "NexaPresenceMachine",
    "NexaPresenceSnapshot",
    "NexaPresenceTransitionError",
    "NexaStatusAdapter",
    "StaticNexaStatusAdapter",
]
