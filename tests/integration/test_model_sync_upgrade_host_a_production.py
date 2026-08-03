from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard import proof
from dokploy_wizard.dokploy.coder_migration_api import CoderMigrationApi
from dokploy_wizard.dokploy.coder_migration_types import CoderId, CoderTemplate
from dokploy_wizard.proof.model_sync_lifecycle_schema import SingleHostLifecycleReceipt
from dokploy_wizard.proof.model_sync_results import ProofTransport
from dokploy_wizard.proof.model_sync_upgrade_host_a_coder import CoderUpgradeClient
from dokploy_wizard.proof.model_sync_upgrade_host_a_observation_collector import _migration
from dokploy_wizard.proof.model_sync_upgrade_host_a_observations import (
    CatalogObservation,
    HostAObservation,
    MigrationObservation,
    ReleaseObservation,
    ScheduleObservation,
    SyncResultObservation,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_process import (
    ModifyCommandObservation,
    ModifyExecutionObservation,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_production import (
    ProductionUpgradeConfig,
    ProductionUpgradeHostAOperations,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    UpgradeHostABinding,
    UpgradeHostAError,
)
from dokploy_wizard.proof.mutation_registry import StrictMutationError
from dokploy_wizard.proof.strict_lease import strict_proof_lease

_BLOCKED_CODE = "CODER_RETIRED_WORKSPACE_NOT_STOPPED"
_RETAINED_NAMES = (
    "ubuntu-vscode-hermes",
    "ubuntu-vscode-kdense-byok",
    "ubuntu-vscode-opencode-pi",
    "ubuntu-vscode-opencode-web",
)


def _observation(
    *,
    commit: str = "a" * 40,
    mutations: tuple[str, ...] = (),
    sync_results: tuple[SyncResultObservation, ...] = (),
    catalog_exact: bool = True,
) -> HostAObservation:
    catalog_sha = "b" * 64
    return HostAObservation(
        release=ReleaseObservation(
            commit,
            "c" * 64,
            "d" * 64,
            Path(f"/root/dokploy-wizard/releases/{'c' * 64}"),
        ),
        migration=MigrationObservation(
            "completed",
            "e" * 64,
            mutations,
            "ubuntu-vscode-opencode-pi",
            _RETAINED_NAMES,
            ("ubuntu-vscode-openwork", "ubuntu-vscode-pi-web"),
        ),
        catalog=CatalogObservation(
            catalog_sha,
            Path("/var/lib/docker/volumes/catalog/_data/generations/generation.json"),
            catalog_sha if catalog_exact else "f" * 64,
            catalog_sha,
            Path("/var/lib/docker/volumes/catalog/_data/state.json"),
        ),
        schedule=ScheduleObservation(
            "1" * 64,
            "1" * 64,
            "2" * 64,
            "2" * 64,
            "3" * 64,
        ),
        sync_results=sync_results,
    )


def _config(tmp_path: Path) -> ProductionUpgradeConfig:
    lifecycle = SingleHostLifecycleReceipt(
        phase="baseline_epoch",
        machine_sha256="1" * 64,
        ssh_sha256="2" * 64,
        architecture="amd64",
        baseline_boot_sha256="3" * 64,
        current_boot_sha256="3" * 64,
        baseline_epoch_id="4" * 64,
        host_a_epoch_id=None,
        teardown_epoch_id=None,
        final_epoch_id=None,
        previous_receipt_sha256=None,
        evidence_sha256="5" * 64,
        namespace_resource_absence_verified=True,
        fresh_install_epoch_verified=False,
        temporal_clean_epoch_evidence=False,
    )
    return ProductionUpgradeConfig(
        host="fixture-host",
        password="fixture-password",
        wrapper=tmp_path / "dokploy-wizard-remote",
        env_file=tmp_path / "install.env",
        artifact_dir=tmp_path,
        binding=UpgradeHostABinding(
            "6" * 64,
            "7" * 64,
            "8" * 64,
            lifecycle,
            "9" * 64,
            0o600,
            "a" * 40,
        ),
        namespace=proof.ProofNamespace("stack", (), (), (), (), ()),
        transport=ProofTransport(
            None, None, "example.test", None, None, None, None, None, None, False
        ),
    )


def _coder() -> CoderUpgradeClient:
    return CoderUpgradeClient.__new__(CoderUpgradeClient)


def test_absent_migration_observation_does_not_create_state(tmp_path: Path) -> None:
    # Given
    migration_dir = tmp_path / "coder-template-migration"

    # When
    observed = _migration(tmp_path)

    # Then
    assert observed is None
    assert not migration_dir.exists()


def test_blocked_modify_rejects_authoritative_observation_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    before = _observation()
    after = _observation(catalog_exact=False)
    operations = ProductionUpgradeHostAOperations(_config(tmp_path), _coder())
    monkeypatch.setattr(
        ProductionUpgradeHostAOperations,
        "_run_modify",
        lambda _self: ModifyExecutionObservation(
            ModifyCommandObservation(1, _BLOCKED_CODE, None, ()), before, after
        ),
    )

    # When / Then
    with pytest.raises(UpgradeHostAError, match="blocked.*observation"):
        operations.modify()


def test_successful_modify_rejects_active_release_commit_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    before = _observation()
    after = _observation(commit="b" * 40)
    operations = ProductionUpgradeHostAOperations(_config(tmp_path), _coder())
    monkeypatch.setattr(
        ProductionUpgradeHostAOperations,
        "_run_modify",
        lambda _self: ModifyExecutionObservation(
            ModifyCommandObservation(0, None, "modify", ("coder",)), before, after
        ),
    )

    # When / Then
    with pytest.raises(UpgradeHostAError, match="release commit"):
        operations.modify()


def test_successful_modify_returns_authoritative_receipt_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    sync_result = SyncResultObservation(
        "4" * 64,
        2,
        Path("/var/lib/docker/volumes/catalog/_data/lease-results/result.json"),
    )
    before = _observation()
    after = _observation(mutations=("f" * 64,), sync_results=(sync_result,))
    operations = ProductionUpgradeHostAOperations(_config(tmp_path), _coder())
    monkeypatch.setattr(
        ProductionUpgradeHostAOperations,
        "_run_modify",
        lambda _self: ModifyExecutionObservation(
            ModifyCommandObservation(0, None, "modify", ("coder",)), before, after
        ),
    )

    # When
    attempt = operations.modify()

    # Then
    assert attempt.deployed_commit == "a" * 40
    assert attempt.control_plane_mutations == 1
    assert attempt.synchronizer_durable_writes == 2


def test_coder_snapshot_exactness_comes_from_current_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    api = CoderMigrationApi.__new__(CoderMigrationApi)
    templates = tuple(
        CoderTemplate(CoderId(str(index)), CoderId("organization"), name)
        for index, name in enumerate(_RETAINED_NAMES, start=1)
    )
    monkeypatch.setattr(CoderMigrationApi, "list_templates", lambda _self, _id: templates)
    monkeypatch.setattr(CoderMigrationApi, "list_workspaces", lambda _self: ())
    client = CoderUpgradeClient(api, "organization", lambda _template, _workspace: None)

    # When
    snapshot = client.snapshot(_observation(catalog_exact=False))

    # Then
    assert snapshot.catalog_exact is False
    assert snapshot.schedule_exact is True
    assert snapshot.source_exact is True


@pytest.mark.parametrize(
    ("mutations", "sync_results", "expected_control", "expected_sync"),
    (
        (("f" * 64,), (), 1, 0),
        (
            (),
            (
                SyncResultObservation(
                    "4" * 64,
                    2,
                    Path("/var/lib/docker/volumes/catalog/_data/lease-results/result.json"),
                ),
            ),
            0,
            2,
        ),
    ),
)
def test_strict_rerun_records_and_rejects_authoritative_nonzero_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutations: tuple[str, ...],
    sync_results: tuple[SyncResultObservation, ...],
    expected_control: int,
    expected_sync: int,
) -> None:
    # Given
    before = _observation()
    after = _observation(mutations=mutations, sync_results=sync_results)
    operations = ProductionUpgradeHostAOperations(_config(tmp_path), _coder())
    monkeypatch.setattr(
        ProductionUpgradeHostAOperations,
        "_run_modify",
        lambda _self: ModifyExecutionObservation(
            ModifyCommandObservation(0, None, "noop", ()), before, after
        ),
    )

    # When / Then
    with strict_proof_lease(tmp_path / "strict.lock") as recorder:
        with pytest.raises(StrictMutationError) as caught:
            operations._strict_rerun(recorder)
    assert caught.value.totals.control_plane_mutations == expected_control
    assert caught.value.totals.synchronizer_durable_writes == expected_sync
