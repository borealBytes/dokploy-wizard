"""Task 1 result payloads, Coder pagination, and the bounded remote collector."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import assert_never

import dokploy_wizard.proof.model_sync_state as state
from dokploy_wizard import proof
from dokploy_wizard.proof import (
    REQUIRED_RESULT_KEYS as REQUIRED_RESULT_KEYS,
)
from dokploy_wizard.proof import (
    build_result as build_result,
)
from dokploy_wizard.proof import (
    derive_result_from_attestation as derive_result_from_attestation,
)
from dokploy_wizard.proof import (
    result_bytes_from_attestation as result_bytes_from_attestation,
)
from dokploy_wizard.proof import (
    run_bounded_process as run_bounded_process,
)
from dokploy_wizard.proof import (
    validate_attestation as validate_attestation,
)
from dokploy_wizard.proof import (
    verify_result_bytes as verify_result_bytes,
)
from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    sha256_bytes,
    unlink_exact_regular_bytes,
    write_or_verify_exact_bytes,
)
from dokploy_wizard.proof.model_sync_preflight_payload import PREFLIGHT_SCRIPT

__all__ = ("PREFLIGHT_SCRIPT", "REQUIRED_RESULT_KEYS")


@dataclass(frozen=True, slots=True, repr=False)
class ProofTransport:
    """Secret-bearing inputs retained only at the local proof transport boundary."""

    cloudflare_account_id: str | None
    cloudflare_zone_id: str | None
    cloudflare_zone_name: str
    cloudflare_token: str | None
    dokploy_api_url: str | None
    dokploy_api_key: str | None
    coder_email: str | None
    coder_hostname: str | None
    coder_password: str | None
    tailscale_required: bool
    dokploy_admin_email: str | None = None
    dokploy_admin_password: str | None = None


@dataclass(frozen=True, slots=True)
class HostIdentity:
    machine_sha256: str
    ssh_sha256: str
    architecture: str


def assert_namespace_identity(*, host_a: HostIdentity, host_b: HostIdentity) -> None:
    """Require two physical hosts with one supported architecture before upload."""
    supported = {"amd64", "arm64"}
    if host_a.machine_sha256 == host_b.machine_sha256 or host_a.ssh_sha256 == host_b.ssh_sha256:
        raise ValueError("Host A and Host B must have distinct machine and SSH identities")
    if host_a.architecture not in supported or host_b.architecture not in supported:
        raise ValueError("Host architecture is unsupported")
    if host_a.architecture != host_b.architecture:
        raise ValueError("Host A and Host B architectures must match")


def assert_followup_proof_contract(
    *,
    contract_name: str,
    receipts: tuple[str, ...],
    host_identity_mode: proof.HostIdentityMode = "distinct",
) -> None:
    distinct = {
        "upgrade_host_a_contract": frozenset({"host-a-baseline"}),
        "final_proof_contract": frozenset({"host-a-destroyed"}),
        "reseed_pair_contract": frozenset({"host-b-clean"}),
    }
    sequential = {
        "upgrade_host_a_contract": frozenset({"single-host-lifecycle-baseline"}),
        "final_proof_contract": frozenset(
            {
                "single-host-lifecycle-host-a",
                "single-host-lifecycle-teardown",
                "single-host-lifecycle-final",
            }
        ),
    }
    match host_identity_mode:
        case "distinct":
            contracts = distinct
        case "single_sequential":
            contracts = sequential
        case unexpected:
            assert_never(unexpected)
    required = contracts.get(contract_name)
    if required is None:
        raise ValueError("unknown or mode-incompatible followup proof contract")
    missing = required - set(receipts)
    if missing:
        raise ValueError(f"{contract_name} requires receipts {sorted(missing)}")


def remove_authorized_outputs(
    paths: proof.ProofRecoveryPaths,
    attestation: proof.BaselineAttestation,
    *,
    boundary_hook: proof.BoundaryHook = proof.ignore_finalization_boundary,
) -> None:
    for name, path in proof.output_paths(paths, attestation.output_sha256).items():
        if not os.path.lexists(path):
            continue
        content = proof.read_proof_bytes(path, 16 * 1024 * 1024, 0o600)
        if sha256_bytes(content) != attestation.output_sha256[name]:
            raise proof.AbortGuardError("generated output bytes are not authorized for rollback")
        try:
            unlink_exact_regular_bytes(path, content)
        except CaptureSchemaError as error:
            raise proof.AbortGuardError("generated output changed during rollback") from error
        boundary_hook(proof.FinalizationBoundary.ROLLBACK_OUTPUT_UNLINKED)
    expected_result = proof.result_bytes_from_attestation(attestation)
    proof.require_generated_bounds({}, expected_result)
    try:
        unlink_exact_regular_bytes(paths.output, expected_result)
    except CaptureSchemaError as error:
        raise proof.AbortGuardError("result output changed during rollback") from error


def complete_resumable_finalization(
    recovery: proof.ProofRecovery,
    *,
    boundary_hook: proof.BoundaryHook = proof.ignore_finalization_boundary,
) -> None:
    if recovery.claim is None or not recovery.resumable:
        raise proof.AbortGuardError("no resumable finalization is active")
    guard = state.read_abort_guard(recovery.paths.guard_path)
    if guard.attestation is None:
        raise proof.AbortGuardError("finalization intent has no attestation")
    result = proof.result_bytes_from_attestation(guard.attestation)
    proof.require_generated_bounds({}, result)
    write_or_verify_exact_bytes(recovery.paths.output, result)
    boundary_hook(proof.FinalizationBoundary.RESULT_PUBLISHED)
    proof.verify_attestation(
        recovery.paths,
        state.read_abort_guard(recovery.paths.guard_path),
        require_result=True,
    )
    state.complete_abort_guard(recovery.paths.guard_path, claim_token=recovery.claim.token)
    boundary_hook(proof.FinalizationBoundary.COMPLETE_GUARD)


def collect_coder_array_pages(
    fetch: Callable[[str], JsonValue], path: str, label: str
) -> list[tuple[int, list[JsonValue]]]:
    pages: list[tuple[int, list[JsonValue]]] = []
    offset = 0
    while True:
        raw = fetch(path.format(offset=offset))
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError(f"Coder {label} API returned an invalid page")
        pages.append((offset, raw))
        if len(raw) < 100:
            return pages
        offset += len(raw)


def collect_coder_workspace_pages(fetch: Callable[[str], JsonValue]) -> list[JsonValue]:
    offset = 0
    expected_count: int | None = None
    workspaces: list[JsonValue] = []
    while expected_count is None or len(workspaces) < expected_count:
        raw = fetch(f"/api/v2/workspaces?q=&limit=100&offset={offset}")
        if not isinstance(raw, dict) or not isinstance(raw.get("count"), int) or raw["count"] < 0:
            raise ValueError("Coder workspace API returned an invalid response")
        page = raw.get("workspaces")
        if not isinstance(page, list) or len(page) > 100:
            raise ValueError("Coder workspace API returned an invalid response")
        expected_count = raw["count"] if expected_count is None else expected_count
        if raw["count"] != expected_count or (not page and expected_count != 0):
            raise ValueError("Coder workspace pagination is incomplete")
        workspaces.extend(page)
        offset += len(page)
    if len(workspaces) != expected_count:
        raise ValueError("Coder workspace pagination count mismatch")
    return workspaces
