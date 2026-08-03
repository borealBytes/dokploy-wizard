from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard.proof import JsonValue
from dokploy_wizard.state.task1_legacy_authority_proof import (
    Task1LegacyAuthorityImportError,
    parse_task1_legacy_authority_bundle,
)
from tests.unit.task1_legacy_authority_shapes import RETAINED_STATE_FILES
from tests.unit.task1_legacy_authority_support import build_import_fixture, json_bytes, with_bundle


def _mutate_json(content: bytes, key: str, value: JsonValue) -> bytes:
    payload: JsonValue = json.loads(content)
    assert isinstance(payload, dict)
    payload[key] = value
    return json_bytes(payload)


def test_v3_bundle_parses_complete_chain_and_bound_ledger(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)

    proven = parse_task1_legacy_authority_bundle(fixture.request.bundle)

    assert proven.ownership_ledger == fixture.ledger
    assert proven.lifecycle.phase == "baseline_epoch"
    assert proven.state_files == RETAINED_STATE_FILES


def test_v3_bundle_accepts_future_final_epoch_chain(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path, lifecycle_phase="final_epoch")

    proven = parse_task1_legacy_authority_bundle(fixture.request.bundle)

    assert proven.lifecycle.phase == "final_epoch"


def test_v3_bundle_rejects_tampered_guard(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)
    tampered = with_bundle(
        fixture,
        abort_guard_bytes=_mutate_json(
            fixture.request.bundle.abort_guard_bytes, "guard_id", "f" * 64
        ),
    )

    with pytest.raises(Task1LegacyAuthorityImportError):
        parse_task1_legacy_authority_bundle(tampered.request.bundle)


def test_v3_bundle_rejects_tampered_result(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)
    tampered = with_bundle(
        fixture,
        result_bytes=_mutate_json(fixture.request.bundle.result_bytes, "proof_commit", "f" * 40),
    )

    with pytest.raises(Task1LegacyAuthorityImportError, match="result"):
        parse_task1_legacy_authority_bundle(tampered.request.bundle)


@pytest.mark.parametrize(
    "field",
    ["host_a_preflight_bytes", "baseline_bytes", "lifecycle_bytes"],
)
def test_v3_bundle_rejects_each_result_bound_artifact_hash(
    tmp_path: Path, field: str
) -> None:
    fixture = build_import_fixture(tmp_path)
    changed = with_bundle(fixture, **{field: getattr(fixture.request.bundle, field) + b" "})

    with pytest.raises(Task1LegacyAuthorityImportError, match="hash"):
        parse_task1_legacy_authority_bundle(changed.request.bundle)


def test_v3_bundle_rejects_non_clean_host_preflight(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path, preflight_overrides={"namespace_clean": False})

    with pytest.raises(Task1LegacyAuthorityImportError, match="clean"):
        parse_task1_legacy_authority_bundle(fixture.request.bundle)


def test_v3_bundle_rejects_target_resource_hidden_behind_clean_flag(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path, preflight_target_cloudflare=True)

    with pytest.raises(Task1LegacyAuthorityImportError, match="target Cloudflare"):
        parse_task1_legacy_authority_bundle(fixture.request.bundle)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "phase": "host_a_epoch",
        },
        {"namespace_resource_absence_verified": False},
        {"fresh_install_epoch_verified": True},
        {"temporal_clean_epoch_evidence": True},
        {"host_a_epoch_id": "a" * 64},
        {"previous_receipt_sha256": "b" * 64},
    ],
)
def test_v3_bundle_rejects_weak_lifecycle_contract(
    tmp_path: Path, overrides: dict[str, JsonValue]
) -> None:
    fixture = build_import_fixture(tmp_path, lifecycle_overrides=overrides)

    with pytest.raises(Task1LegacyAuthorityImportError, match="lifecycle"):
        parse_task1_legacy_authority_bundle(fixture.request.bundle)


def test_v3_bundle_rejects_ledger_hash_drift(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path)
    drifted_ledger = fixture.request.bundle.ownership_ledger_bytes.replace(
        b"wizard-nextcloud-data", b"wizard-nextcloud-drift"
    )
    changed = with_bundle(fixture, ownership_ledger_bytes=drifted_ledger)

    with pytest.raises(Task1LegacyAuthorityImportError, match="ledger"):
        parse_task1_legacy_authority_bundle(changed.request.bundle)


@pytest.mark.parametrize(
    "resources",
    [
        [],
        ["ownership-ledger.json", "ownership-ledger.json"],
        ["../ownership-ledger.json"],
        ["state/ownership-ledger.json"],
        ["state\\ownership-ledger.json"],
        ["ownership-ledger.txt"],
        ["desired-state.json"],
        ["ownership-ledger.json", "applied-state.json"],
        [".json", "ownership-ledger.json"],
    ],
)
def test_v3_bundle_rejects_invalid_baseline_state_file_inventory(
    tmp_path: Path, resources: list[str]
) -> None:
    fixture = build_import_fixture(tmp_path, baseline_resource_override=resources)

    with pytest.raises(Task1LegacyAuthorityImportError, match="state-file"):
        parse_task1_legacy_authority_bundle(fixture.request.bundle)


def test_v3_bundle_rejects_noncanonical_json_boundary(tmp_path: Path) -> None:
    fixture = build_import_fixture(tmp_path, baseline_noncanonical=True)

    with pytest.raises(Task1LegacyAuthorityImportError, match="canonical"):
        parse_task1_legacy_authority_bundle(fixture.request.bundle)
