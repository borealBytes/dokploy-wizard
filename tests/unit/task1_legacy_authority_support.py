from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Literal, assert_never

from dokploy_wizard.proof import (
    AbortGuard,
    BaselineResultEvidence,
    JsonValue,
    abort_guard_payload,
    build_baseline_attestation,
    canonical_json_bytes,
    result_bytes_from_attestation,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceV1,
)
from dokploy_wizard.state import OwnedResource, OwnershipLedger
from dokploy_wizard.state.task1_legacy_authority_import import (
    Task1LegacyAuthorityImportRequest,
)
from dokploy_wizard.state.task1_legacy_authority_proof import Task1LegacyAuthorityBundle
from dokploy_wizard.state.uninstall_provenance import (
    ProviderCreationDisposition,
    ProviderCreationResult,
)
from dokploy_wizard.state.upgrade_io import canonical_bytes
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    active_receipt,
    cleanup_report,
)
from tests.unit.task1_legacy_authority_shapes import RETAINED_STATE_FILES

OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


@dataclass(frozen=True, slots=True)
class ImportFixture:
    request: Task1LegacyAuthorityImportRequest
    ledger: OwnershipLedger


def json_bytes(value: dict[str, JsonValue]) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def build_import_fixture(
    tmp_path: Path,
    *,
    preflight_overrides: dict[str, JsonValue] | None = None,
    baseline_resource_override: list[str] | None = None,
    lifecycle_overrides: dict[str, JsonValue] | None = None,
    ledger_bytes_override: bytes | None = None,
    observations_override: tuple[ProviderCreationResult, ...] | None = None,
    baseline_noncanonical: bool = False,
    preflight_target_cloudflare: bool = False,
    lifecycle_phase: Literal["baseline_epoch", "final_epoch"] = "baseline_epoch",
) -> ImportFixture:
    service = OwnedResource(
        "nextcloud_service", "dokploy-compose:compose-1:nextcloud", "stack:wizard:nextcloud"
    )
    volume = OwnedResource(
        "nextcloud_volume", "wizard-nextcloud-data", "stack:wizard:nextcloud"
    )
    ledger = OwnershipLedger(format_version=1, resources=(service, volume))
    ledger_bytes = canonical_bytes(ledger.to_dict())
    supplied_ledger_bytes = ledger_bytes if ledger_bytes_override is None else ledger_bytes_override
    cloudflare_resources = (
        [
            {
                "fingerprint_sha256": "f" * 64,
                "id": "target-id",
                "kind": "tunnel",
                "match": "exact",
                "provenance": "preexisting_unowned",
            }
        ]
        if preflight_target_cloudflare
        else []
    )
    preflight: dict[str, JsonValue] = {
        "architecture": "amd64",
        "boot_sha256": "1" * 64,
        "host_identities_distinct": False,
        "host_identity_mode": "single_sequential",
        "inventory": {
            "cloudflare": {
                "resources": cloudflare_resources,
                "state": "present" if cloudflare_resources else "absent",
            },
            "coder": {"resources": [], "state": "absent"},
            "docker": {
                "provenance": "docker_absent_clean",
                "resources": [],
                "state": "absent",
            },
            "dokploy": {"resources": [], "state": "absent"},
            "tailscale": {"resources": [], "state": "absent"},
        },
        "machine_sha256": "2" * 64,
        "namespace_clean": True,
        "provenance_role": "host_a",
        "schema_version": 1,
        "ssh_sha256": "3" * 64,
        "temporal_clean_epoch_evidence": False,
    }
    if preflight_overrides is not None:
        preflight.update(preflight_overrides)
    baseline: dict[str, JsonValue] = {
        "cloudflare": [],
        "cloudflare_cleanup_receipt": {},
        "cloudflare_final_journal_sha256": "4" * 64,
        "coder_secrets": [],
        "legacy_workspace_managed_fingerprints": [],
        "post_cleanup_cloudflare": {},
        "post_install_cloudflare": {},
        "pre_install_cloudflare": {},
        "preexisting_cloudflare": [],
        "resource_inventories": {
            "tailscale": {"identifiers": []},
            "wizard_state": {
                "ledger_sha256": sha256(supplied_ledger_bytes).hexdigest(),
                "resources": (
                    list(RETAINED_STATE_FILES)
                    if baseline_resource_override is None
                    else baseline_resource_override
                ),
                "state_sha256": "5" * 64,
            },
        },
        "stack_name": "wizard",
        "templates": [],
        "workspaces": [],
    }
    lifecycle_identity: dict[str, JsonValue] = {
        "architecture": "amd64",
        "baseline_boot_sha256": "1" * 64,
        "baseline_epoch_id": "6" * 64,
        "host_identity_mode": "single_sequential",
        "machine_sha256": "2" * 64,
        "namespace_resource_absence_verified": True,
        "schema_version": 1,
        "ssh_sha256": "3" * 64,
    }
    lifecycle: dict[str, JsonValue]
    match lifecycle_phase:
        case "baseline_epoch":
            lifecycle = dict(lifecycle_identity)
            lifecycle.update({
                "current_boot_sha256": "1" * 64,
                "evidence_sha256": sha256(json_bytes(preflight)).hexdigest(),
                "final_epoch_id": None,
                "fresh_install_epoch_verified": False,
                "host_a_epoch_id": None,
                "phase": "baseline_epoch",
                "previous_receipt_sha256": None,
                "teardown_epoch_id": None,
                "temporal_clean_epoch_evidence": False,
            })
        case "final_epoch":
            lifecycle = dict(lifecycle_identity)
            lifecycle.update({
                "current_boot_sha256": "7" * 64,
                "evidence_sha256": "8" * 64,
                "final_epoch_id": "9" * 64,
                "fresh_install_epoch_verified": True,
                "host_a_epoch_id": "a" * 64,
                "phase": "final_epoch",
                "previous_receipt_sha256": "b" * 64,
                "teardown_epoch_id": "c" * 64,
                "temporal_clean_epoch_evidence": True,
            })
        case unexpected:
            assert_never(unexpected)
    if lifecycle_overrides is not None:
        lifecycle.update(lifecycle_overrides)
    preflight_bytes = json_bytes(preflight)
    baseline_bytes = json_bytes(baseline)
    if baseline_noncanonical:
        baseline_bytes = baseline_bytes.replace(b'"cloudflare":[]', b'"cloudflare": [ ]')
    lifecycle_bytes = json_bytes(lifecycle)
    receipt = active_receipt(tmp_path)
    assert receipt.context_evidence is not None
    snapshot_evidence = Task1CloudflareSnapshotEvidenceV1.from_cleanup_report(
        cleanup_report(receipt.context_evidence.context_sha256), receipt.context_evidence
    )
    evidence = BaselineResultEvidence(
        source_base_commit="d" * 40,
        proof_commit="e" * 40,
        host_identity_mode="single_sequential",
        images={
            "coder": "coder@sha256:" + "1" * 64,
            "litellm": "litellm@sha256:" + "2" * 64,
            "pgvector": "pgvector@sha256:" + "3" * 64,
            "redis": "redis@sha256:" + "4" * 64,
            "postfix": "postfix@sha256:" + "5" * 64,
        },
        env_receipt=receipt,
        guard_path=tmp_path / "abort-guard.json",
        artifact_dir=tmp_path,
        abort_guard_sha256="f" * 64,
        host_a_preflight_sha256=sha256(preflight_bytes).hexdigest(),
        host_b_preflight_sha256=None,
        single_host_lifecycle_sha256=sha256(lifecycle_bytes).hexdigest(),
        baseline_sha256=sha256(baseline_bytes).hexdigest(),
        protected_artifacts_before_sha256="6" * 64,
        coder_secret_inventory_sha256="7" * 64,
        legacy_workspace_managed_fingerprints_sha256="8" * 64,
        preexisting_cloudflare_sha256="9" * 64,
        post_install_cloudflare_sha256=snapshot_evidence.post_install_snapshot_sha256,
        cloudflare_snapshot_evidence=snapshot_evidence,
    )
    attestation = build_baseline_attestation(
        evidence, guard_id="a" * 64, result_path=tmp_path / "result.json"
    )
    guard = AbortGuard(
        "a" * 64, "armed", "complete", "plan", None, None, None, receipt, attestation
    )
    observations = observations_override or (
        ProviderCreationResult(
            ProviderCreationDisposition.CREATED,
            service,
            OWNER,
            "dokploy_compose",
            "compose-1",
            "project-1",
            "d" * 64,
        ),
        ProviderCreationResult(
            ProviderCreationDisposition.CREATED,
            volume,
            OWNER,
            "docker_volume",
            "volume-1",
            "docker",
            "e" * 64,
        ),
    )
    bundle = Task1LegacyAuthorityBundle(
        abort_guard_bytes=json_bytes(abort_guard_payload(guard)),
        result_bytes=result_bytes_from_attestation(attestation),
        host_a_preflight_bytes=preflight_bytes,
        baseline_bytes=baseline_bytes,
        lifecycle_bytes=lifecycle_bytes,
        ownership_ledger_bytes=supplied_ledger_bytes,
    )
    return ImportFixture(Task1LegacyAuthorityImportRequest(bundle, observations), ledger)


def with_bundle(
    fixture: ImportFixture, **changes: bytes
) -> ImportFixture:
    return replace(
        fixture,
        request=replace(
            fixture.request,
            bundle=replace(fixture.request.bundle, **changes),
        ),
    )
