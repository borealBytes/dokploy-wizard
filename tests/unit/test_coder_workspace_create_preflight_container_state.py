from __future__ import annotations

import pytest

from dokploy_wizard.packs.coder.reconciler import CoderError
from dokploy_wizard.proof import model_sync_coder_api


@pytest.mark.parametrize(
    ("states", "stage"),
    [
        (b"", "container_missing"),
        (b"exited\n", "container_not_running"),
        (b"restarting\n", "container_restarting"),
        (b"exited\ndead\n", "container_ambiguous"),
    ],
)
def test_internal_route_classifies_unresolved_container_from_all_states(
    monkeypatch: pytest.MonkeyPatch, states: bytes, stage: model_sync_coder_api.CoderSnapshotStage
) -> None:
    # Given
    calls: list[list[str]] = []

    def observe(command: list[str], **_kwargs: str | int | bytes) -> bytes:
        calls.append(command)
        return states

    monkeypatch.setattr(model_sync_coder_api, "_coder_container_name", lambda _service: None)
    monkeypatch.setattr(model_sync_coder_api, "run_bounded_process", observe)

    # When / Then
    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api._internal_base_url("proof-stack")

    assert error.value.stage == stage
    assert calls == [
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.service=proof-stack-coder",
            "--format",
            "{{.State}}",
        ]
    ]


def test_internal_route_classifies_running_state_after_resolution_failure_as_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def unresolved(_service: str) -> str | None:
        raise CoderError("opaque")

    monkeypatch.setattr(model_sync_coder_api, "_coder_container_name", unresolved)
    monkeypatch.setattr(
        model_sync_coder_api,
        "run_bounded_process",
        lambda *_args, **_kwargs: b"running\nexited\n",
    )

    # When / Then
    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api._internal_base_url("proof-stack")

    assert error.value.stage == "container_discovery_inconsistent"
    assert "opaque" not in str(error.value)


@pytest.mark.parametrize("states", [b"unknown\n", b"\xff"])
def test_internal_route_classifies_all_container_projection_failure_without_output(
    monkeypatch: pytest.MonkeyPatch, states: bytes
) -> None:
    # Given
    monkeypatch.setattr(model_sync_coder_api, "_coder_container_name", lambda _service: None)
    monkeypatch.setattr(
        model_sync_coder_api,
        "run_bounded_process",
        lambda *_args, **_kwargs: states,
    )

    # When / Then
    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api._internal_base_url("proof-stack")

    assert error.value.stage == "container_discovery_unavailable"
    assert "unknown" not in str(error.value)


def test_internal_route_classifies_all_container_observation_failure_without_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(model_sync_coder_api, "_coder_container_name", lambda _service: None)
    monkeypatch.setattr(
        model_sync_coder_api,
        "run_bounded_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("opaque")),
    )

    # When / Then
    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api._internal_base_url("proof-stack")

    assert error.value.stage == "container_discovery_unavailable"
    assert "opaque" not in str(error.value)
