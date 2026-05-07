from __future__ import annotations

import inspect

import pytest

from dokploy_wizard.litellm.admin import (
    LiteLLMGatewayManager,
    LiteLLMReadinessError,
    LiteLLMTeamRecord,
    LiteLLMVirtualKeyRecord,
)


class FakeReadinessApi:
    def __init__(self, snapshots: list[dict[str, object]]) -> None:
        self._snapshots = list(snapshots)
        self.calls = 0

    def readiness(self) -> dict[str, object]:
        self.calls += 1
        if not self._snapshots:
            return {"status": "error", "db": "Not connected"}
        return self._snapshots.pop(0)

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return ()

    def create_team(self, *, team_alias: str, models: tuple[str, ...]) -> LiteLLMTeamRecord:
        raise AssertionError(f"unexpected create_team call for {team_alias}: {models}")

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        return ()

    def create_key(self, **_: object) -> LiteLLMVirtualKeyRecord:
        raise AssertionError("unexpected create_key call")


class FakeTransientReadinessApi(FakeReadinessApi):
    def __init__(self) -> None:
        super().__init__([{"status": "healthy", "db": "connected"}])
        self._failed_once = False

    def readiness(self) -> dict[str, object]:
        if not self._failed_once:
            self._failed_once = True
            self.calls += 1
            raise ConnectionResetError(104, "Connection reset by peer")
        return super().readiness()


def test_litellm_readiness_gate_retries_until_healthy() -> None:
    api = FakeReadinessApi(
        [
            {"status": "starting", "db": "Not connected"},
            {"status": "connected", "db": "Not connected"},
            {"status": "healthy", "db": "connected"},
        ]
    )
    sleeps: list[float] = []

    LiteLLMGatewayManager(api=api, sleep_fn=sleeps.append).wait_until_ready(
        attempts=5,
        delay_seconds=0.25,
    )

    assert api.calls == 3
    assert sleeps == [0.25, 0.25]


def test_litellm_readiness_gate_retries_transient_connection_errors() -> None:
    api = FakeTransientReadinessApi()
    sleeps: list[float] = []

    LiteLLMGatewayManager(api=api, sleep_fn=sleeps.append).wait_until_ready(
        attempts=3,
        delay_seconds=0.5,
    )

    assert api.calls == 2
    assert sleeps == [0.5]


def test_litellm_readiness_gate_uses_extended_first_boot_defaults() -> None:
    signature = inspect.signature(LiteLLMGatewayManager.wait_until_ready)

    assert signature.parameters["attempts"].default == 120
    assert signature.parameters["delay_seconds"].default == 5.0


def test_litellm_readiness_gate_times_out_with_actionable_error() -> None:
    api = FakeReadinessApi(
        [{"status": "starting", "db": "Not connected"}] * 3,
    )

    with pytest.raises(LiteLLMReadinessError) as error:
        LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None).wait_until_ready(
            attempts=3,
            delay_seconds=0,
        )

    message = str(error.value)
    assert "LiteLLM did not become ready before the timeout" in message
    assert "/health/readiness" in message
    assert "shared-core Postgres connectivity" in message
    assert "status='starting'" in message
    assert "db='Not connected'" in message
