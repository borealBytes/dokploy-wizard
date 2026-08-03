"""Bounded verification of Task 1 V3 legacy uninstall-authority evidence."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_lifecycle_schema import SingleHostLifecycleReceipt
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence_schema import (
    Task1CloudflareSnapshotEvidenceError,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError
from dokploy_wizard.state.models import OwnershipLedger
from dokploy_wizard.state.task1_legacy_authority_artifacts import (
    Task1LegacyArtifactError,
    parse_task1_legacy_artifacts,
    proof_document,
)
from dokploy_wizard.state.uninstall_authority_schema import UninstallAuthorityError

_MAX_GUARD_BYTES: Final = 256 * 1024
_MAX_RESULT_BYTES: Final = 256 * 1024


class Task1LegacyAuthorityImportError(UninstallAuthorityError):
    """Raised when legacy authority is not proven by one complete V3 chain."""


@dataclass(frozen=True, slots=True)
class Task1LegacyAuthorityBundle:
    """Caller-supplied immutable bytes for one complete Task 1 V3 proof."""

    abort_guard_bytes: bytes
    result_bytes: bytes
    host_a_preflight_bytes: bytes
    baseline_bytes: bytes
    lifecycle_bytes: bytes
    ownership_ledger_bytes: bytes

    def __post_init__(self) -> None:
        if not all(type(value) is bytes for value in (
            self.abort_guard_bytes, self.result_bytes, self.host_a_preflight_bytes,
            self.baseline_bytes, self.lifecycle_bytes, self.ownership_ledger_bytes,
        )):
            raise Task1LegacyAuthorityImportError("Task 1 authority bundle accepts bytes only.")


@dataclass(frozen=True, slots=True)
class ProvenTask1LegacyAuthority:
    """Parsed evidence safe for authority-observation validation."""

    ownership_ledger: OwnershipLedger
    lifecycle: SingleHostLifecycleReceipt
    state_files: tuple[str, ...]
    attestation_sha256: str


def parse_task1_legacy_authority_bundle(
    bundle: Task1LegacyAuthorityBundle,
) -> ProvenTask1LegacyAuthority:
    """Parse each bounded document once and verify the complete V3 trust chain."""

    try:
        guard_payload = proof_document(bundle.abort_guard_bytes, _MAX_GUARD_BYTES, "abort guard")
        result_payload = proof_document(bundle.result_bytes, _MAX_RESULT_BYTES, "result")
    except Task1LegacyArtifactError as error:
        raise Task1LegacyAuthorityImportError(str(error)) from error
    try:
        guard = proof.parse_abort_guard(guard_payload)
        result = proof.build_result(result_payload)
    except (ValueError, Task1ProofContextError, Task1CloudflareSnapshotEvidenceError) as error:
        raise Task1LegacyAuthorityImportError(
            "Task 1 guard or result schema is invalid."
        ) from error
    attestation = guard.attestation
    if (
        guard.state != "armed"
        or guard.phase != "complete"
        or guard.claimant_kind != "plan"
        or attestation is None
        or attestation.context_evidence is None
        or attestation.to_payload()["schema_version"] != 3
        or result["schema_version"] != 3
    ):
        raise Task1LegacyAuthorityImportError("Task 1 abort guard is not terminal V3 proof.")
    try:
        proof.verify_result_bytes(attestation, bundle.result_bytes)
    except ValueError as error:
        raise Task1LegacyAuthorityImportError("Task 1 result bytes are not attested.") from error
    if result["abort_guard_sha256"] != proof.abort_guard_sha256(attestation):
        raise Task1LegacyAuthorityImportError("Task 1 result abort guard hash drifted.")
    _artifact_hash(result, "host_a_preflight_sha256", bundle.host_a_preflight_bytes)
    _artifact_hash(result, "baseline_sha256", bundle.baseline_bytes)
    _artifact_hash(result, "single_host_lifecycle_sha256", bundle.lifecycle_bytes)
    try:
        artifacts = parse_task1_legacy_artifacts(bundle)
    except Task1LegacyArtifactError as error:
        raise Task1LegacyAuthorityImportError(str(error)) from error
    return ProvenTask1LegacyAuthority(
        artifacts.ownership_ledger,
        artifacts.lifecycle,
        artifacts.state_files,
        proof.abort_guard_sha256(attestation),
    )


def _artifact_hash(result: dict[str, proof.JsonValue], key: str, content: bytes) -> None:
    expected = result[key]
    if not isinstance(expected, str) or sha256(content).hexdigest() != expected:
        raise Task1LegacyAuthorityImportError(f"Task 1 {key} artifact hash drifted.")
