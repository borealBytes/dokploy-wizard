"""Versioned hash-only Cloudflare snapshot evidence bound into Task 1 V3 proof state."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence import (
    validate_cleanup_evidence,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema_types import (
    CloudflareSnapshotError,
    canonical_snapshot_bytes,
    require_snapshot_hash,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import (
    Task1ProofContextEvidenceV1,
    validate_task1_context_evidence,
)

_FIELDS: Final = (
    "context_sha256",
    "pre_install_snapshot_sha256",
    "post_install_snapshot_sha256",
    "post_cleanup_snapshot_sha256",
    "otp_provider_sha256",
    "final_journal_sha256",
    "cleanup_receipt_sha256",
)
_PAYLOAD_KEYS: Final = frozenset({"schema_version", *_FIELDS})


class Task1CloudflareSnapshotEvidenceError(CloudflareSnapshotError):
    """Raised when snapshot evidence cannot bind a canonical cleanup report."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Task1CloudflareSnapshotEvidenceV1:
    """Hash-only V1 binding of one validated Task 1 Cloudflare cleanup report."""

    schema_version: Literal[1] = 1
    context_sha256: str
    pre_install_snapshot_sha256: str
    post_install_snapshot_sha256: str
    post_cleanup_snapshot_sha256: str
    otp_provider_sha256: str
    final_journal_sha256: str
    cleanup_receipt_sha256: str

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "context_sha256": self.context_sha256,
            "pre_install_snapshot_sha256": self.pre_install_snapshot_sha256,
            "post_install_snapshot_sha256": self.post_install_snapshot_sha256,
            "post_cleanup_snapshot_sha256": self.post_cleanup_snapshot_sha256,
            "otp_provider_sha256": self.otp_provider_sha256,
            "final_journal_sha256": self.final_journal_sha256,
            "cleanup_receipt_sha256": self.cleanup_receipt_sha256,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, JsonValue]) -> "Task1CloudflareSnapshotEvidenceV1":
        if frozenset(value) != _PAYLOAD_KEYS:
            raise Task1CloudflareSnapshotEvidenceError("snapshot evidence schema is invalid")
        if value.get("schema_version") != 1:
            raise Task1CloudflareSnapshotEvidenceError("snapshot evidence schema is invalid")
        evidence = cls(
            context_sha256=_hash_field(value["context_sha256"]),
            pre_install_snapshot_sha256=_hash_field(value["pre_install_snapshot_sha256"]),
            post_install_snapshot_sha256=_hash_field(value["post_install_snapshot_sha256"]),
            post_cleanup_snapshot_sha256=_hash_field(value["post_cleanup_snapshot_sha256"]),
            otp_provider_sha256=_hash_field(value["otp_provider_sha256"]),
            final_journal_sha256=_hash_field(value["final_journal_sha256"]),
            cleanup_receipt_sha256=_hash_field(value["cleanup_receipt_sha256"]),
        )
        validate_task1_cloudflare_snapshot_evidence(evidence)
        return evidence

    @classmethod
    def from_cleanup_report(
        cls,
        report: Task1CloudflareCleanupReport,
        context_evidence: Task1ProofContextEvidenceV1,
    ) -> "Task1CloudflareSnapshotEvidenceV1":
        validate_task1_context_evidence(context_evidence)
        pre_install, post_install, post_cleanup = _validated_report_snapshots(report)
        if (
            pre_install.context_sha256 != context_evidence.context_sha256
            or post_install.context_sha256 != context_evidence.context_sha256
            or post_cleanup.context_sha256 != context_evidence.context_sha256
            or pre_install.otp_provider_sha256 != post_install.otp_provider_sha256
            or pre_install.otp_provider_sha256 != post_cleanup.otp_provider_sha256
        ):
            raise Task1CloudflareSnapshotEvidenceError(
                "snapshot evidence does not bind the Task 1 context"
            )
        evidence = cls(
            context_sha256=context_evidence.context_sha256,
            pre_install_snapshot_sha256=pre_install.snapshot_sha256,
            post_install_snapshot_sha256=post_install.snapshot_sha256,
            post_cleanup_snapshot_sha256=post_cleanup.snapshot_sha256,
            otp_provider_sha256=pre_install.otp_provider_sha256,
            final_journal_sha256=report.final_journal_sha256,
            cleanup_receipt_sha256=hashlib.sha256(report.receipt_bytes).hexdigest(),
        )
        validate_task1_cloudflare_snapshot_evidence(evidence)
        return evidence


def validate_task1_cloudflare_snapshot_evidence(
    evidence: Task1CloudflareSnapshotEvidenceV1,
) -> None:
    """Require the exact V1 version and seven nonzero lower-case SHA-256 values."""
    if evidence.schema_version != 1:
        raise Task1CloudflareSnapshotEvidenceError("snapshot evidence version is invalid")
    for value in (
        evidence.context_sha256,
        evidence.pre_install_snapshot_sha256,
        evidence.post_install_snapshot_sha256,
        evidence.post_cleanup_snapshot_sha256,
        evidence.otp_provider_sha256,
        evidence.final_journal_sha256,
        evidence.cleanup_receipt_sha256,
    ):
        try:
            require_snapshot_hash(value)
        except CloudflareSnapshotError as error:
            raise Task1CloudflareSnapshotEvidenceError(
                "snapshot evidence hash is invalid"
            ) from error


def validate_baseline_snapshot_evidence_payload(
    value: Mapping[str, JsonValue],
    evidence: Task1CloudflareSnapshotEvidenceV1,
) -> None:
    """Require a baseline payload to contain exactly the snapshots hashed by V1 evidence."""
    validate_task1_cloudflare_snapshot_evidence(evidence)
    required = {
        "cloudflare_cleanup_receipt",
        "cloudflare_final_journal_sha256",
        "post_cleanup_cloudflare",
        "post_install_cloudflare",
        "pre_install_cloudflare",
    }
    if not required <= set(value):
        raise Task1CloudflareSnapshotEvidenceError("baseline snapshot evidence is incomplete")
    pre_install = _snapshot_from_payload(value["pre_install_cloudflare"])
    post_install = _snapshot_from_payload(value["post_install_cloudflare"])
    post_cleanup = _snapshot_from_payload(value["post_cleanup_cloudflare"])
    receipt = value["cloudflare_cleanup_receipt"]
    journal_hash = value["cloudflare_final_journal_sha256"]
    if not isinstance(receipt, Mapping) or not isinstance(journal_hash, str):
        raise Task1CloudflareSnapshotEvidenceError("baseline snapshot evidence is invalid")
    receipt_bytes = canonical_snapshot_bytes(dict(receipt))
    if (
        pre_install.context_sha256 != evidence.context_sha256
        or post_install.context_sha256 != evidence.context_sha256
        or post_cleanup.context_sha256 != evidence.context_sha256
        or pre_install.snapshot_sha256 != evidence.pre_install_snapshot_sha256
        or post_install.snapshot_sha256 != evidence.post_install_snapshot_sha256
        or post_cleanup.snapshot_sha256 != evidence.post_cleanup_snapshot_sha256
        or pre_install.otp_provider_sha256 != evidence.otp_provider_sha256
        or post_install.otp_provider_sha256 != evidence.otp_provider_sha256
        or post_cleanup.otp_provider_sha256 != evidence.otp_provider_sha256
        or journal_hash != evidence.final_journal_sha256
        or hashlib.sha256(receipt_bytes).hexdigest() != evidence.cleanup_receipt_sha256
    ):
        raise Task1CloudflareSnapshotEvidenceError("baseline snapshot evidence drifted")
    validate_cleanup_evidence(pre_install, post_cleanup, receipt_bytes, journal_hash)


def _validated_report_snapshots(
    report: Task1CloudflareCleanupReport,
) -> tuple[CloudflareSnapshotV1, CloudflareSnapshotV1, CloudflareSnapshotV1]:
    pre_install = CloudflareSnapshotV1.from_bytes(report.pre_install.to_bytes())
    post_install = CloudflareSnapshotV1.from_bytes(report.post_install.to_bytes())
    post_cleanup = CloudflareSnapshotV1.from_bytes(report.post_cleanup.to_bytes())
    if report.post_install_snapshot_sha256 != post_install.snapshot_sha256:
        raise Task1CloudflareSnapshotEvidenceError("cleanup report post-install hash drifted")
    validate_cleanup_evidence(
        pre_install,
        post_cleanup,
        report.receipt_bytes,
        report.final_journal_sha256,
    )
    return pre_install, post_install, post_cleanup


def _snapshot_from_payload(value: JsonValue) -> CloudflareSnapshotV1:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise Task1CloudflareSnapshotEvidenceError("baseline snapshot payload is invalid")
    return CloudflareSnapshotV1.from_bytes(canonical_snapshot_bytes(dict(value)))


def _hash_field(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise Task1CloudflareSnapshotEvidenceError("snapshot evidence fields are invalid")
    return value
