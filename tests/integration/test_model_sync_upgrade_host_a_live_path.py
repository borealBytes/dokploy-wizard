from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

import dokploy_wizard.remote as remote_cli
from dokploy_wizard.dokploy.coder_migration_api import CoderMigrationApi
from dokploy_wizard.proof.model_sync_upgrade_host_a_coder import CoderUpgradeClient
from dokploy_wizard.proof.model_sync_upgrade_host_a_process import ModifyCommandObservation
from dokploy_wizard.proof.model_sync_upgrade_host_a_production import (
    ProductionUpgradeHostAOperations,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError
from tests.integration._model_sync_upgrade_host_a_live_path_support import (
    BLOCKED_CODE,
    FINAL_COMMIT,
    ModifyExecutionFixture,
    SequencedTransport,
    observation,
    observation_payload,
    production_config,
    write_wrapper,
)


def test_production_modify_command_requests_and_parses_verbose_remote_evidence(
    tmp_path: Path,
) -> None:
    # Given
    config = production_config(tmp_path)
    write_wrapper(config.wrapper)
    operations = ProductionUpgradeHostAOperations(
        config,
        CoderUpgradeClient.__new__(CoderUpgradeClient),
    )

    # When
    result = operations._run_modify()

    # Then
    assert result.command.lifecycle_mode == "modify"
    assert result.command.phases_to_run == ("shared_core", "coder")
    assert result.before.release.commit_sha == FINAL_COMMIT
    assert result.after.release.commit_sha == FINAL_COMMIT


def test_remote_blocked_modify_collects_only_after_final_release_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    env_file = tmp_path / "install.env"
    env_file.write_text("ROOT_DOMAIN=example.test\n", encoding="utf-8")
    payload = json.dumps(observation_payload(), sort_keys=True, separators=(",", ":"))
    transport = SequencedTransport((payload + "\n").encode())
    monkeypatch.setattr(
        "dokploy_wizard.remote.ParamikoRemoteTransport.connect",
        lambda **_kwargs: transport,
    )
    monkeypatch.setattr(
        remote_cli,
        "_upload_remote_bundle",
        lambda **_kwargs: SimpleNamespace(
            archive_sha256="b" * 64,
            bootstrap_sha256="c" * 64,
        ),
    )

    # When
    exit_code = remote_cli.main(
        [
            "modify",
            "--host",
            "example.test",
            "--password",
            "fixture-password",
            "--env-file",
            str(env_file),
            "--deploy-commit",
            FINAL_COMMIT,
            "--verbose",
            "--capture-upgrade-observations",
        ]
    )

    # Then
    assert exit_code == 1
    assert transport.events == [
        "activate-release",
        "modify-observation-before",
        "modify",
        "modify-observation-after",
        "close",
    ]
    assert "--task18-force-model-sync-upgrade" in shlex.split(transport.commands["modify"])


def test_blocked_modify_allows_only_release_activation_when_managed_state_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    before = observation("9" * 40)
    after = observation(FINAL_COMMIT)
    operations = ProductionUpgradeHostAOperations(
        production_config(tmp_path),
        CoderUpgradeClient.__new__(CoderUpgradeClient),
    )
    monkeypatch.setattr(
        ProductionUpgradeHostAOperations,
        "_run_modify",
        lambda _self: ModifyExecutionFixture(
            ModifyCommandObservation(1, BLOCKED_CODE, None, ()),
            before,
            after,
        ),
    )

    # When
    result = operations.modify()

    # Then
    assert result.exit_code == 1
    assert result.failure_code == BLOCKED_CODE
    assert result.control_plane_mutations == 0
    assert result.synchronizer_durable_writes == 0


def test_blocked_modify_rejects_release_drift_away_from_exact_final_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    before = observation(FINAL_COMMIT)
    after = observation("8" * 40)
    operations = ProductionUpgradeHostAOperations(
        production_config(tmp_path),
        CoderUpgradeClient.__new__(CoderUpgradeClient),
    )
    monkeypatch.setattr(
        ProductionUpgradeHostAOperations,
        "_run_modify",
        lambda _self: ModifyExecutionFixture(
            ModifyCommandObservation(1, BLOCKED_CODE, None, ()),
            before,
            after,
        ),
    )

    # When / Then
    with pytest.raises(UpgradeHostAError, match="blocked Host A observation drifted"):
        operations.modify()


def test_pre_upgrade_snapshot_does_not_require_remote_collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    api = CoderMigrationApi.__new__(CoderMigrationApi)
    monkeypatch.setattr(CoderUpgradeClient, "_inventory", lambda _self: ("primary", (), "f" * 64))
    client = CoderUpgradeClient(api, "organization", lambda _template, _workspace: None)
    operations = ProductionUpgradeHostAOperations(production_config(tmp_path), client)

    # When
    result = operations.snapshot()

    # Then
    assert result.primary_uuid == "primary"
    assert result.catalog_exact is False
    assert result.schedule_exact is False
    assert result.source_exact is False
