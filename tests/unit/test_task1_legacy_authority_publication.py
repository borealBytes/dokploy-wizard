from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.state.task1_legacy_authority_import import import_task1_legacy_authority
from dokploy_wizard.state.task1_legacy_authority_proof import Task1LegacyAuthorityImportError
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_provenance import ProviderCreationDisposition
from dokploy_wizard.state.upgrade import upgrade_state_contract
from tests.unit.task1_legacy_authority_support import build_import_fixture


def _write_current_ledger(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "ownership-ledger.json"
    path.write_bytes(content)
    return path


def test_import_accepts_retained_baseline_epoch_and_publishes_bound_observations(
    tmp_path: Path,
) -> None:
    fixture = build_import_fixture(tmp_path)
    ledger_path = _write_current_ledger(
        tmp_path, fixture.request.bundle.ownership_ledger_bytes
    )

    imported = import_task1_legacy_authority(tmp_path, ledger_path, fixture.request)

    assert len(imported) == len(fixture.ledger.resources)
    assert all(
        UninstallAuthorityStore(tmp_path).load_created(item)
        for item in fixture.ledger.resources
    )


@pytest.mark.parametrize("kind", ["duplicate", "partial", "extra"])
def test_import_rejects_non_bijective_observation_coverage(
    tmp_path: Path, kind: Literal["duplicate", "partial", "extra"]
) -> None:
    fixture = build_import_fixture(tmp_path)
    observations = fixture.request.observations
    match kind:
        case "duplicate":
            changed = (*observations, observations[0])
        case "partial":
            changed = observations[:1]
        case "extra":
            changed = (
                *observations,
                replace(
                    observations[0],
                    resource=replace(observations[0].resource, resource_id="extra"),
                ),
            )
        case unexpected:
            assert_never(unexpected)
    request = replace(fixture.request, observations=changed)
    ledger_path = _write_current_ledger(tmp_path, request.bundle.ownership_ledger_bytes)

    with pytest.raises(Task1LegacyAuthorityImportError, match="observation"):
        import_task1_legacy_authority(tmp_path, ledger_path, request)

    assert not (tmp_path / "uninstall-authority.json").exists()


@pytest.mark.parametrize(
    "disposition",
    [ProviderCreationDisposition.REUSED, ProviderCreationDisposition.UPDATED],
)
def test_import_rejects_non_created_observation(
    tmp_path: Path, disposition: ProviderCreationDisposition
) -> None:
    fixture = build_import_fixture(tmp_path)
    request = replace(
        fixture.request,
        observations=(
            replace(fixture.request.observations[0], disposition=disposition),
            *fixture.request.observations[1:],
        ),
    )
    ledger_path = _write_current_ledger(tmp_path, request.bundle.ownership_ledger_bytes)

    with pytest.raises(Task1LegacyAuthorityImportError, match="CREATED"):
        import_task1_legacy_authority(tmp_path, ledger_path, request)


def test_import_rejects_wrong_provider_family(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)
    request = replace(
        fixture.request,
        observations=(
            replace(fixture.request.observations[0], provider="docker_volume"),
            *fixture.request.observations[1:],
        ),
    )
    ledger_path = _write_current_ledger(tmp_path, request.bundle.ownership_ledger_bytes)

    with pytest.raises(Task1LegacyAuthorityImportError, match="provider"):
        import_task1_legacy_authority(tmp_path, ledger_path, request)


def test_import_rejects_current_ledger_byte_drift(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)
    ledger_path = _write_current_ledger(
        tmp_path, fixture.request.bundle.ownership_ledger_bytes + b" "
    )

    with pytest.raises(Task1LegacyAuthorityImportError, match="current ownership ledger"):
        import_task1_legacy_authority(tmp_path, ledger_path, fixture.request)


def test_import_rejects_partial_preexisting_receipt_drift_before_new_write(
    tmp_path: Path,
) -> None:
    fixture = build_import_fixture(tmp_path)
    ledger_path = _write_current_ledger(tmp_path, fixture.request.bundle.ownership_ledger_bytes)
    first, second = fixture.request.observations
    store = UninstallAuthorityStore(tmp_path)
    store.record_created(
        resource=first.resource,
        owner_id=first.owner_id,
        provider=first.provider,
        physical_target_id="drifted",
        parent_target_id=first.parent_target_id,
        expected_fingerprint=first.expected_fingerprint,
    )
    before = (tmp_path / "uninstall-authority.json").read_bytes()

    with pytest.raises(Task1LegacyAuthorityImportError, match="drift"):
        import_task1_legacy_authority(tmp_path, ledger_path, fixture.request)

    assert (tmp_path / "uninstall-authority.json").read_bytes() == before
    assert store.load_created(second.resource) is None


def test_import_exact_second_replay_is_idempotent(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)
    ledger_path = _write_current_ledger(tmp_path, fixture.request.bundle.ownership_ledger_bytes)
    first = import_task1_legacy_authority(tmp_path, ledger_path, fixture.request)
    before = (tmp_path / "uninstall-authority.json").read_bytes()

    second = import_task1_legacy_authority(tmp_path, ledger_path, fixture.request)

    assert second == first
    assert (tmp_path / "uninstall-authority.json").read_bytes() == before


def test_upgrade_seam_imports_before_upgrade_state_mutation(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)
    ledger_path = _write_current_ledger(tmp_path, fixture.request.bundle.ownership_ledger_bytes)
    paths = {
        "raw_input": tmp_path / "raw-input.json",
        "desired": tmp_path / "desired-state.json",
        "applied": tmp_path / "applied-state.json",
        "ledger": ledger_path,
        "litellm_keys": tmp_path / "litellm-generated-keys.json",
        "surfsense_secrets": tmp_path / "surfsense-generated-secrets.json",
        "seaweedfs_secrets": tmp_path / "seaweedfs-generated-secrets.json",
    }
    for key, path in paths.items():
        if key != "ledger":
            path.write_text(key, encoding="utf-8")

    upgrade_state_contract(
        state_dir=tmp_path,
        owner_id="3b8e1e83-0e57-4d66-a65e-1edbf2aac838",
        paths=paths,
        legacy_authority_import=fixture.request,
    )

    assert (tmp_path / "uninstall-authority.json").exists()
    assert (tmp_path / "state-upgrade-intent-v1.json").exists()
