"""Strict schema for immutable single-host lifecycle receipts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, assert_never

from dokploy_wizard import proof

LifecyclePhase = Literal["baseline_epoch", "host_a_epoch", "teardown_verified", "final_epoch"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "architecture",
    "baseline_boot_sha256",
    "baseline_epoch_id",
    "current_boot_sha256",
    "evidence_sha256",
    "final_epoch_id",
    "fresh_install_epoch_verified",
    "host_a_epoch_id",
    "host_identity_mode",
    "machine_sha256",
    "namespace_resource_absence_verified",
    "phase",
    "previous_receipt_sha256",
    "schema_version",
    "ssh_sha256",
    "teardown_epoch_id",
    "temporal_clean_epoch_evidence",
}


@dataclass(frozen=True, slots=True)
class SingleHostLifecycleReceipt:
    phase: LifecyclePhase
    machine_sha256: str
    ssh_sha256: str
    architecture: str
    baseline_boot_sha256: str
    current_boot_sha256: str
    baseline_epoch_id: str
    host_a_epoch_id: str | None
    teardown_epoch_id: str | None
    final_epoch_id: str | None
    previous_receipt_sha256: str | None
    evidence_sha256: str
    namespace_resource_absence_verified: bool
    fresh_install_epoch_verified: bool
    temporal_clean_epoch_evidence: bool
    host_identity_mode: Literal["single_sequential"] = "single_sequential"

    def to_payload(self) -> dict[str, proof.JsonValue]:
        return {
            "architecture": self.architecture,
            "baseline_boot_sha256": self.baseline_boot_sha256,
            "baseline_epoch_id": self.baseline_epoch_id,
            "current_boot_sha256": self.current_boot_sha256,
            "evidence_sha256": self.evidence_sha256,
            "final_epoch_id": self.final_epoch_id,
            "fresh_install_epoch_verified": self.fresh_install_epoch_verified,
            "host_a_epoch_id": self.host_a_epoch_id,
            "host_identity_mode": self.host_identity_mode,
            "machine_sha256": self.machine_sha256,
            "namespace_resource_absence_verified": (self.namespace_resource_absence_verified),
            "phase": self.phase,
            "previous_receipt_sha256": self.previous_receipt_sha256,
            "schema_version": 1,
            "ssh_sha256": self.ssh_sha256,
            "teardown_epoch_id": self.teardown_epoch_id,
            "temporal_clean_epoch_evidence": self.temporal_clean_epoch_evidence,
        }

    def to_bytes(self) -> bytes:
        return proof.canonical_json_bytes(self.to_payload()) + b"\n"


def parse_lifecycle_receipt(value: proof.JsonValue) -> SingleHostLifecycleReceipt:
    if not isinstance(value, dict) or set(value) != _FIELDS or value["schema_version"] != 1:
        raise ValueError("single-host lifecycle receipt schema is invalid")
    receipt = SingleHostLifecycleReceipt(
        phase=_phase(value["phase"]),
        machine_sha256=_hash(value["machine_sha256"]),
        ssh_sha256=_hash(value["ssh_sha256"]),
        architecture=_text(value["architecture"]),
        baseline_boot_sha256=_hash(value["baseline_boot_sha256"]),
        current_boot_sha256=_hash(value["current_boot_sha256"]),
        baseline_epoch_id=_hash(value["baseline_epoch_id"]),
        host_a_epoch_id=_optional_hash(value["host_a_epoch_id"]),
        teardown_epoch_id=_optional_hash(value["teardown_epoch_id"]),
        final_epoch_id=_optional_hash(value["final_epoch_id"]),
        previous_receipt_sha256=_optional_hash(value["previous_receipt_sha256"]),
        evidence_sha256=_hash(value["evidence_sha256"]),
        namespace_resource_absence_verified=_boolean(value["namespace_resource_absence_verified"]),
        fresh_install_epoch_verified=_boolean(value["fresh_install_epoch_verified"]),
        temporal_clean_epoch_evidence=_boolean(value["temporal_clean_epoch_evidence"]),
        host_identity_mode=_single_mode(value["host_identity_mode"]),
    )
    validate_lifecycle_receipt(receipt)
    return receipt


def validate_lifecycle_receipt(receipt: SingleHostLifecycleReceipt) -> None:
    for value in (
        receipt.machine_sha256,
        receipt.ssh_sha256,
        receipt.baseline_boot_sha256,
        receipt.current_boot_sha256,
        receipt.baseline_epoch_id,
        receipt.evidence_sha256,
    ):
        _hash(value)
    for optional in (
        receipt.host_a_epoch_id,
        receipt.teardown_epoch_id,
        receipt.final_epoch_id,
        receipt.previous_receipt_sha256,
    ):
        _optional_hash(optional)
    if receipt.architecture not in {"amd64", "arm64"}:
        raise ValueError("single-host lifecycle architecture is unsupported")
    match receipt.phase:
        case "baseline_epoch":
            valid = (
                receipt.host_a_epoch_id is None
                and receipt.teardown_epoch_id is None
                and receipt.final_epoch_id is None
                and receipt.previous_receipt_sha256 is None
                and receipt.current_boot_sha256 == receipt.baseline_boot_sha256
                and receipt.namespace_resource_absence_verified
                and not receipt.fresh_install_epoch_verified
                and not receipt.temporal_clean_epoch_evidence
            )
        case "host_a_epoch":
            valid = (
                receipt.host_a_epoch_id is not None
                and receipt.teardown_epoch_id is None
                and receipt.final_epoch_id is None
                and receipt.previous_receipt_sha256 is not None
                and not receipt.namespace_resource_absence_verified
                and not receipt.fresh_install_epoch_verified
                and not receipt.temporal_clean_epoch_evidence
            )
        case "teardown_verified":
            valid = (
                receipt.host_a_epoch_id is not None
                and receipt.teardown_epoch_id is not None
                and receipt.final_epoch_id is None
                and receipt.previous_receipt_sha256 is not None
                and receipt.namespace_resource_absence_verified
                and not receipt.fresh_install_epoch_verified
                and not receipt.temporal_clean_epoch_evidence
            )
        case "final_epoch":
            valid = (
                receipt.host_a_epoch_id is not None
                and receipt.teardown_epoch_id is not None
                and receipt.final_epoch_id is not None
                and receipt.previous_receipt_sha256 is not None
                and receipt.current_boot_sha256 != receipt.baseline_boot_sha256
                and receipt.namespace_resource_absence_verified
                and receipt.fresh_install_epoch_verified
                and receipt.temporal_clean_epoch_evidence
            )
        case unexpected:
            assert_never(unexpected)
    if not valid:
        raise ValueError("single-host lifecycle phase fields are inconsistent")


def _phase(value: proof.JsonValue) -> LifecyclePhase:
    match value:
        case "baseline_epoch":
            return "baseline_epoch"
        case "host_a_epoch":
            return "host_a_epoch"
        case "teardown_verified":
            return "teardown_verified"
        case "final_epoch":
            return "final_epoch"
        case _:
            raise ValueError("single-host lifecycle phase is invalid")


def _hash(value: proof.JsonValue) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value) or value == "0" * 64:
        raise ValueError("single-host lifecycle hash is invalid")
    return value


def _optional_hash(value: proof.JsonValue) -> str | None:
    return None if value is None else _hash(value)


def _text(value: proof.JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("single-host lifecycle text is invalid")
    return value


def _boolean(value: proof.JsonValue) -> bool:
    if not isinstance(value, bool):
        raise ValueError("single-host lifecycle boolean is invalid")
    return value


def _single_mode(value: proof.JsonValue) -> Literal["single_sequential"]:
    if value != "single_sequential":
        raise ValueError("single-host lifecycle mode is invalid")
    return "single_sequential"
