from __future__ import annotations

import stat
from pathlib import Path

import pytest

from dokploy_wizard.proof import model_sync_task1_cloudflare_journal as cleanup_cli
from dokploy_wizard.proof.model_sync_task1_cloudflare_cleanup_report import (
    Task1CloudflareCleanupRun,
)
from dokploy_wizard.state import RawEnvInput
from dokploy_wizard.state.dokploy_runtime_auth import (
    DokployRuntimeAuth,
    persist_dokploy_runtime_auth,
)
from tests.helpers.task1_runtime_auth import prepare_task1_runtime_auth_fixture


def test_cleanup_rejects_upload_hash_drift_before_loading_runtime_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    fixture = prepare_task1_runtime_auth_fixture(tmp_path)
    upload_path = fixture.preparation.uploaded_env_file
    upload_path.write_bytes(
        fixture.upload_bytes.replace(
            b"ROOT_DOMAIN=example.test\n",
            b"ROOT_DOMAIN=drifted.test\n",
        )
    )

    def reject_runtime_auth_load(_state_dir: Path) -> None:
        raise AssertionError("runtime auth must load only after upload validation")

    monkeypatch.setattr(
        "dokploy_wizard.state.dokploy_runtime_auth.load_dokploy_runtime_auth",
        reject_runtime_auth_load,
    )

    # When
    exit_code = cleanup_cli.main(
        [
            "--env-file",
            str(upload_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--task1-proof-context",
            str(fixture.preparation.context_file),
        ]
    )

    # Then
    assert exit_code == 1
    assert "generated-runtime-key" not in capsys.readouterr().err


def test_cleanup_scope_uses_original_cloudflare_inputs_after_runtime_auth_merge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    fixture = prepare_task1_runtime_auth_fixture(tmp_path)
    upload_path = fixture.preparation.uploaded_env_file
    state_dir = tmp_path / "state"
    persist_dokploy_runtime_auth(
        state_dir,
        DokployRuntimeAuth(
            api_url="http://127.0.0.1:3000",
            api_key="generated-runtime-key",
        ),
    )
    seen: dict[str, str] = {}

    class RecordingCloudflareBackend:
        def __init__(self, raw_env: RawEnvInput) -> None:
            seen["account_id"] = raw_env.values["CLOUDFLARE_ACCOUNT_ID"]
            seen["zone_id"] = raw_env.values["CLOUDFLARE_ZONE_ID"]
            seen["api_token"] = raw_env.values["CLOUDFLARE_API_TOKEN"]

    class RecordingSnapshotBackend:
        def __init__(self, raw_env: RawEnvInput) -> None:
            assert raw_env.values["CLOUDFLARE_ACCOUNT_ID"] == "original-account"

    def capture_cleanup(run: Task1CloudflareCleanupRun) -> dict[str, str]:
        seen["scope_account_id"] = run.scope.account_id
        seen["scope_zone_id"] = run.scope.zone_id
        return {"status": "cleaned"}

    monkeypatch.setattr(
        "dokploy_wizard.networking.cloudflare.CloudflareApiBackend",
        RecordingCloudflareBackend,
    )
    monkeypatch.setattr(
        "dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_api."
        "CloudflareSnapshotApiBackend",
        RecordingSnapshotBackend,
    )
    monkeypatch.setattr(
        cleanup_cli.Task1CloudflareJournal,
        "for_context",
        lambda **_: None,
    )
    monkeypatch.setattr(cleanup_cli, "run_task1_cloudflare_cleanup", capture_cleanup)

    # When
    exit_code = cleanup_cli.main(
        [
            "--env-file",
            str(upload_path),
            "--state-dir",
            str(state_dir),
            "--task1-proof-context",
            str(fixture.preparation.context_file),
        ]
    )

    # Then
    assert exit_code == 0
    assert seen == {
        "account_id": "original-account",
        "zone_id": "original-zone",
        "api_token": "cloudflare-token",
        "scope_account_id": "original-account",
        "scope_zone_id": "original-zone",
    }
    assert upload_path.read_bytes() == fixture.upload_bytes
    assert stat.S_IMODE(upload_path.stat().st_mode) == fixture.upload_mode
