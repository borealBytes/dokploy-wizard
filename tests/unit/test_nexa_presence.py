# pyright: reportMissingImports=false

from __future__ import annotations

import pytest

from dokploy_wizard.packs.openclaw.nexa_presence import (
    NexaMappedStatus,
    NexaPresenceMachine,
    NexaPresenceTransitionError,
    StaticNexaStatusAdapter,
)


def test_presence_defaults_to_idle_without_external_status_support() -> None:
    machine = NexaPresenceMachine()

    snapshot = machine.snapshot()

    assert snapshot.state == "idle"
    assert snapshot.failure_reason is None
    assert snapshot.external_supported is False
    assert snapshot.external_status is None
    assert snapshot.external_message is None


def test_presence_lifecycle_transitions_are_explicit_and_deterministic() -> None:
    machine = NexaPresenceMachine()

    assert machine.enqueue().state == "queued"
    assert machine.start_work().state == "working"
    assert machine.start_refresh().state == "refreshing"
    assert machine.finish_refresh().state == "working"
    assert machine.finish().state == "done"
    assert machine.reset().state == "idle"


def test_presence_failed_state_preserves_reason_and_can_requeue() -> None:
    machine = NexaPresenceMachine()

    machine.enqueue()
    machine.start_work()
    failed = machine.fail(reason="document refresh timed out")

    assert failed.state == "failed"
    assert failed.failure_reason == "document refresh timed out"

    queued = machine.enqueue()

    assert queued.state == "queued"
    assert queued.failure_reason is None


def test_presence_rejects_invalid_transition_instead_of_guessing() -> None:
    machine = NexaPresenceMachine()

    with pytest.raises(NexaPresenceTransitionError, match="Cannot transition Nexa presence"):
        machine.finish()


def test_presence_can_project_internal_states_to_supported_external_statuses() -> None:
    machine = NexaPresenceMachine(
        status_adapter=StaticNexaStatusAdapter(
            {
                "idle": NexaMappedStatus(status="online", message="Idle"),
                "queued": NexaMappedStatus(status="away", message="Queued"),
                "working": NexaMappedStatus(status="dnd", message="Working"),
                "refreshing": NexaMappedStatus(status="away", message="Refreshing"),
                "failed": NexaMappedStatus(status="dnd", message="Failed"),
                "done": NexaMappedStatus(status="online", message="Done"),
            }
        )
    )

    queued = machine.enqueue()
    working = machine.start_work()

    assert queued.external_supported is True
    assert queued.external_status == "away"
    assert queued.external_message == "Queued"
    assert working.external_supported is True
    assert working.external_status == "dnd"
    assert working.external_message == "Working"


def test_presence_degrades_to_internal_only_when_external_status_is_unsupported() -> None:
    machine = NexaPresenceMachine(
        status_adapter=StaticNexaStatusAdapter(
            {
                "working": NexaMappedStatus(status="dnd", message="Working"),
            },
            supported=False,
        )
    )

    machine.enqueue()
    snapshot = machine.start_work()

    assert snapshot.state == "working"
    assert snapshot.external_supported is False
    assert snapshot.external_status is None
    assert snapshot.external_message is None
