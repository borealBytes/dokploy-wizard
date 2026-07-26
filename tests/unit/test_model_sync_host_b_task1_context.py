from __future__ import annotations

import json
from pathlib import Path

import pytest

import dokploy_wizard.proof.model_sync_host_b as model_sync_host_b
import dokploy_wizard.proof.model_sync_remote as model_sync_remote
from dokploy_wizard.proof.model_sync_task1_context import active_task1_proof_context
from tests.helpers.task1_runtime_auth import prepare_task1_runtime_auth_fixture


class SnapshotTransport:
    def close(self) -> None:
        return None


def test_snapshot_cli_activates_validated_task1_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    fixture = prepare_task1_runtime_auth_fixture(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(
        model_sync_host_b,
        "_snapshot",
        lambda _env_file, _state_dir: {"context_active": active_task1_proof_context() is not None},
    )

    # When
    exit_code = model_sync_host_b.main(
        [
            "model-sync-snapshot",
            "--env-file",
            str(fixture.preparation.uploaded_env_file),
            "--state-dir",
            str(state_dir),
            "--task1-proof-context",
            str(fixture.preparation.context_file),
        ]
    )

    # Then
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"context_active": True}


def test_host_a_snapshot_passes_remote_task1_context_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    transport = SnapshotTransport()
    commands: list[str] = []
    monkeypatch.setattr(
        model_sync_remote.ParamikoRemoteTransport,
        "connect",
        lambda **_kwargs: transport,
    )

    def capture(
        _transport: SnapshotTransport,
        command: str,
        *,
        timeout_seconds: int,
    ) -> str:
        del timeout_seconds
        commands.append(command)
        return "{}"

    monkeypatch.setattr(model_sync_remote, "capture_remote_output", capture)

    # When
    result = model_sync_remote.capture_host_a_snapshot(
        host="fixture-host",
        password="fixture-password",
        task1_context=True,
    )

    # Then
    assert result == "{}"
    assert commands == [
        "cd /root/dokploy-wizard && PYTHONPATH=./src python3 -m "
        "dokploy_wizard.proof.model_sync_host_b model-sync-snapshot --env-file .install.env "
        "--state-dir state --task1-proof-context task1-proof-context.json"
    ]


def test_host_a_snapshot_preserves_legacy_remote_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    transport = SnapshotTransport()
    commands: list[str] = []
    monkeypatch.setattr(
        model_sync_remote.ParamikoRemoteTransport,
        "connect",
        lambda **_kwargs: transport,
    )

    def capture(
        _transport: SnapshotTransport,
        command: str,
        *,
        timeout_seconds: int,
    ) -> str:
        del timeout_seconds
        commands.append(command)
        return "{}"

    monkeypatch.setattr(model_sync_remote, "capture_remote_output", capture)

    # When
    result = model_sync_remote.capture_host_a_snapshot(
        host="fixture-host",
        password="fixture-password",
    )

    # Then
    assert result == "{}"
    assert commands == [
        "cd /root/dokploy-wizard && PYTHONPATH=./src python3 -m "
        "dokploy_wizard.proof.model_sync_host_b model-sync-snapshot --env-file .install.env "
        "--state-dir state"
    ]
