"""Sequential single-host lifecycle transitions and durable publication."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof import model_sync_preflight_evidence as identity
from dokploy_wizard.proof.model_sync_identity import RemoteProbe
from dokploy_wizard.proof.model_sync_lifecycle_schema import (
    SingleHostLifecycleReceipt,
    parse_lifecycle_receipt,
    validate_lifecycle_receipt,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class HostLifecycleEpoch:
    epoch_id: str
    expected_previous_sha256: str | None


@dataclass(frozen=True, slots=True)
class FinalEpochEvidence:
    clean_probe: RemoteProbe
    managed_probe: RemoteProbe
    epoch: HostLifecycleEpoch


def build_baseline_epoch(
    probe: RemoteProbe,
    epoch: HostLifecycleEpoch,
) -> SingleHostLifecycleReceipt:
    _require_clean(probe)
    _require_epoch(epoch, previous=None, used=())
    receipt = SingleHostLifecycleReceipt(
        phase="baseline_epoch",
        machine_sha256=probe.machine_sha256,
        ssh_sha256=probe.ssh_sha256,
        architecture=probe.architecture,
        baseline_boot_sha256=probe.boot_sha256,
        current_boot_sha256=probe.boot_sha256,
        baseline_epoch_id=epoch.epoch_id,
        host_a_epoch_id=None,
        teardown_epoch_id=None,
        final_epoch_id=None,
        previous_receipt_sha256=None,
        evidence_sha256=_preflight_sha256(probe),
        namespace_resource_absence_verified=True,
        fresh_install_epoch_verified=False,
        temporal_clean_epoch_evidence=False,
    )
    validate_lifecycle_receipt(receipt)
    return receipt


def record_host_a_epoch(
    previous: SingleHostLifecycleReceipt,
    probe: RemoteProbe,
    epoch: HostLifecycleEpoch,
) -> SingleHostLifecycleReceipt:
    if previous.phase != "baseline_epoch":
        raise ValueError("Host A epoch requires the baseline lifecycle phase")
    _require_previous(previous, epoch)
    _require_same_host(previous, probe)
    if probe.namespace_clean:
        raise ValueError("Host A epoch requires a managed namespace")
    if probe.boot_sha256 != previous.current_boot_sha256:
        raise ValueError("Host A epoch must continue the baseline installation boot")
    _require_epoch(epoch, previous=receipt_sha256(previous), used=(previous.baseline_epoch_id,))
    receipt = replace(
        previous,
        phase="host_a_epoch",
        host_a_epoch_id=epoch.epoch_id,
        previous_receipt_sha256=epoch.expected_previous_sha256,
        evidence_sha256=_preflight_sha256(probe),
        namespace_resource_absence_verified=False,
    )
    validate_lifecycle_receipt(receipt)
    return receipt


def record_teardown_epoch(
    previous: SingleHostLifecycleReceipt,
    probe: RemoteProbe,
    epoch: HostLifecycleEpoch,
) -> SingleHostLifecycleReceipt:
    if previous.phase != "host_a_epoch":
        raise ValueError("teardown requires the Host A lifecycle phase")
    _require_previous(previous, epoch)
    _require_same_host(previous, probe)
    _require_clean(probe)
    used = (previous.baseline_epoch_id, _required_id(previous.host_a_epoch_id))
    _require_epoch(epoch, previous=receipt_sha256(previous), used=used)
    receipt = replace(
        previous,
        phase="teardown_verified",
        current_boot_sha256=probe.boot_sha256,
        teardown_epoch_id=epoch.epoch_id,
        previous_receipt_sha256=epoch.expected_previous_sha256,
        evidence_sha256=_preflight_sha256(probe),
        namespace_resource_absence_verified=True,
    )
    validate_lifecycle_receipt(receipt)
    return receipt


def record_final_epoch(
    previous: SingleHostLifecycleReceipt,
    evidence: FinalEpochEvidence,
) -> SingleHostLifecycleReceipt:
    if previous.phase != "teardown_verified":
        raise ValueError("final epoch requires verified teardown")
    _require_previous(previous, evidence.epoch)
    _require_same_host(previous, evidence.clean_probe)
    _require_same_host(previous, evidence.managed_probe)
    _require_clean(evidence.clean_probe)
    if evidence.managed_probe.namespace_clean:
        raise ValueError("final epoch requires a fresh managed installation")
    if evidence.clean_probe.boot_sha256 != evidence.managed_probe.boot_sha256:
        raise ValueError("final clean and managed captures must share one boot epoch")
    if evidence.clean_probe.boot_sha256 == previous.current_boot_sha256:
        raise ValueError("final proof requires a new boot epoch after teardown")
    used = (
        previous.baseline_epoch_id,
        _required_id(previous.host_a_epoch_id),
        _required_id(previous.teardown_epoch_id),
    )
    _require_epoch(evidence.epoch, previous=receipt_sha256(previous), used=used)
    receipt = replace(
        previous,
        phase="final_epoch",
        current_boot_sha256=evidence.clean_probe.boot_sha256,
        final_epoch_id=evidence.epoch.epoch_id,
        previous_receipt_sha256=evidence.epoch.expected_previous_sha256,
        evidence_sha256=_final_evidence_sha256(evidence),
        namespace_resource_absence_verified=True,
        fresh_install_epoch_verified=True,
        temporal_clean_epoch_evidence=True,
    )
    validate_lifecycle_receipt(receipt)
    return receipt


def receipt_sha256(receipt: SingleHostLifecycleReceipt) -> str:
    return hashlib.sha256(receipt.to_bytes()).hexdigest()


def publish_lifecycle_receipt(
    path: Path,
    receipt: SingleHostLifecycleReceipt,
) -> None:
    validate_lifecycle_receipt(receipt)
    artifacts.write_or_verify_exact_bytes(path, receipt.to_bytes())


def _require_previous(
    previous: SingleHostLifecycleReceipt,
    epoch: HostLifecycleEpoch,
) -> None:
    if epoch.expected_previous_sha256 != receipt_sha256(previous):
        raise ValueError("transition does not bind the previous lifecycle receipt")


def _require_epoch(
    epoch: HostLifecycleEpoch,
    *,
    previous: str | None,
    used: tuple[str, ...],
) -> None:
    if not _SHA256.fullmatch(epoch.epoch_id) or epoch.epoch_id == "0" * 64:
        raise ValueError("lifecycle epoch ID must be a non-zero SHA-256 value")
    if epoch.expected_previous_sha256 != previous:
        raise ValueError("transition does not bind the previous lifecycle receipt")
    if epoch.epoch_id in used:
        raise ValueError("lifecycle epoch ID was already consumed")


def _require_same_host(
    previous: SingleHostLifecycleReceipt,
    probe: RemoteProbe,
) -> None:
    if (
        probe.machine_sha256,
        probe.ssh_sha256,
        probe.architecture,
    ) != (
        previous.machine_sha256,
        previous.ssh_sha256,
        previous.architecture,
    ):
        raise ValueError("single-host lifecycle identity changed")


def _require_clean(probe: RemoteProbe) -> None:
    if not probe.namespace_clean:
        raise ValueError("single-host lifecycle requires a clean namespace")


def _required_id(value: str | None) -> str:
    if value is None:
        raise ValueError("single-host lifecycle is missing a required epoch ID")
    return value


def _preflight_sha256(probe: RemoteProbe) -> str:
    payload = identity.preflight_evidence(
        probe,
        mode="single_sequential",
        role="host_a",
    )
    return artifacts.sha256_bytes(proof.canonical_json_bytes(payload) + b"\n")


def _final_evidence_sha256(evidence: FinalEpochEvidence) -> str:
    payload = {
        "clean_preflight": identity.preflight_evidence(
            evidence.clean_probe,
            mode="single_sequential",
            role="host_a",
        ),
        "managed_snapshot": evidence.managed_probe.to_dict(),
    }
    return artifacts.sha256_bytes(proof.canonical_json_bytes(payload) + b"\n")


__all__ = (
    "FinalEpochEvidence",
    "HostLifecycleEpoch",
    "SingleHostLifecycleReceipt",
    "build_baseline_epoch",
    "parse_lifecycle_receipt",
    "publish_lifecycle_receipt",
    "receipt_sha256",
    "record_final_epoch",
    "record_host_a_epoch",
    "record_teardown_epoch",
)
