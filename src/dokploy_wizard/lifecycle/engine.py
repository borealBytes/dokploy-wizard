"""Thin fixed-order lifecycle engine built on the existing phase reconcilers."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dokploy_wizard.bootstrap import DokployBootstrapBackend, reconcile_dokploy
from dokploy_wizard.core import (
    SharedCoreBackend,
    build_shared_core_ledger,
    reconcile_shared_core,
)
from dokploy_wizard.lifecycle.changes import LifecyclePlan, applicable_phases_for
from dokploy_wizard.networking import (
    CloudflareBackend,
    build_access_ledger,
    build_networking_ledger,
    reconcile_cloudflare_access,
    reconcile_networking,
)
from dokploy_wizard.packs.coder import CoderBackend, build_coder_ledger, reconcile_coder
from dokploy_wizard.packs.headscale import (
    HeadscaleBackend,
    build_headscale_ledger,
    reconcile_headscale,
)
from dokploy_wizard.packs.matrix import MatrixBackend, build_matrix_ledger, reconcile_matrix
from dokploy_wizard.packs.nextcloud import (
    NextcloudBackend,
    build_nextcloud_ledger,
    reconcile_nextcloud,
)
from dokploy_wizard.packs.openclaw import (
    OpenClawBackend,
    build_my_farm_advisor_ledger,
    build_openclaw_ledger,
    reconcile_my_farm_advisor,
    reconcile_openclaw,
)
from dokploy_wizard.packs.seaweedfs import (
    SeaweedFsBackend,
    build_seaweedfs_ledger,
    reconcile_seaweedfs,
)
from dokploy_wizard.preflight import PreflightReport
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
    OwnershipLedger,
    RawEnvInput,
    write_applied_checkpoint,
    write_ownership_ledger,
)
from dokploy_wizard.tailscale import TailscaleBackend, build_tailscale_ledger, reconcile_tailscale


@dataclass(frozen=True)
class LifecycleBackends:
    bootstrap: DokployBootstrapBackend
    tailscale: TailscaleBackend
    networking: CloudflareBackend
    cloudflared: Any | None
    shared_core: SharedCoreBackend
    headscale: HeadscaleBackend
    matrix: MatrixBackend
    nextcloud: NextcloudBackend
    seaweedfs: SeaweedFsBackend
    coder: CoderBackend
    openclaw: OpenClawBackend


def execute_lifecycle_plan(
    *,
    state_dir: Path,
    dry_run: bool,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    preflight_report: PreflightReport,
    lifecycle_plan: LifecyclePlan,
    backends: LifecycleBackends,
) -> dict[str, Any]:
    applicable_phases = lifecycle_plan.applicable_phases
    valid_phases = set(lifecycle_plan.initial_completed_steps)
    phase_results: dict[str, dict[str, Any]] = {}
    current_ledger = ownership_ledger

    phase_results["preflight"] = preflight_report.to_dict()
    if not dry_run and "preflight" not in valid_phases:
        valid_phases.add("preflight")
        _promote_preserved_phases(lifecycle_plan.preserved_phases, applicable_phases, valid_phases)
        _write_checkpoint(state_dir, desired_state, applicable_phases, valid_phases)

    if lifecycle_plan.mode == "noop":
        for phase in applicable_phases[1:]:
            if phase in lifecycle_plan.preserved_phases:
                phase_results[phase] = _preserved_result(
                    phase=phase,
                    raw_env=raw_env,
                    desired_state=desired_state,
                    ownership_ledger=current_ledger,
                    backends=backends,
                )
        return _build_summary(
            lifecycle_plan=lifecycle_plan,
            preflight_report=preflight_report,
            desired_state=desired_state,
            dry_run=dry_run,
            phase_results=phase_results,
            state_status="existing",
        )

    for phase in applicable_phases[1:]:
        if phase not in lifecycle_plan.phases_to_run:
            if phase in lifecycle_plan.preserved_phases:
                phase_results[phase] = _preserved_result(
                    phase=phase,
                    raw_env=raw_env,
                    desired_state=desired_state,
                    ownership_ledger=current_ledger,
                    backends=backends,
                )
            continue
        if phase == "dokploy_bootstrap":
            result = reconcile_dokploy(dry_run=dry_run, backend=backends.bootstrap)
            phase_results[phase] = result.to_dict()
        elif phase == "tailscale":
            tailscale = reconcile_tailscale(
                dry_run=dry_run,
                raw_env=raw_env,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.tailscale,
            )
            phase_results[phase] = tailscale.result.to_dict()
            if not dry_run:
                current_ledger = build_tailscale_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    node_resource_id=tailscale.node_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "networking":
            networking = reconcile_networking(
                dry_run=dry_run,
                raw_env=raw_env,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.networking,
                connector_backend=backends.cloudflared,
            )
            phase_results[phase] = networking.result.to_dict()
            if (
                not dry_run
                and networking.result.connector is not None
                and networking.result.connector.passed
            ):
                _emit_dokploy_ready_hint(networking.result.connector.public_url)
            if not dry_run:
                if networking.tunnel_resource_id is None:
                    raise RuntimeError("Networking reconciliation did not return a tunnel id.")
                current_ledger = build_networking_ledger(
                    existing_ledger=current_ledger,
                    account_id=networking.result.account_id,
                    zone_id=networking.result.zone_id,
                    tunnel_resource_id=networking.tunnel_resource_id,
                    dns_resource_ids=networking.dns_resource_ids,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "shared_core":
            shared_core = reconcile_shared_core(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.shared_core,
            )
            phase_results[phase] = shared_core.result.to_dict()
            if not dry_run:
                current_ledger = build_shared_core_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    network_resource_id=shared_core.network_resource_id,
                    postgres_resource_id=shared_core.postgres_resource_id,
                    redis_resource_id=shared_core.redis_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "headscale":
            headscale = reconcile_headscale(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.headscale,
            )
            phase_results[phase] = headscale.result.to_dict()
            if not dry_run:
                current_ledger = build_headscale_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    service_resource_id=headscale.service_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "matrix":
            matrix = reconcile_matrix(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.matrix,
            )
            phase_results[phase] = matrix.result.to_dict()
            if not dry_run:
                current_ledger = build_matrix_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    service_resource_id=matrix.service_resource_id,
                    data_resource_id=matrix.data_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "seaweedfs":
            seaweedfs = reconcile_seaweedfs(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.seaweedfs,
            )
            phase_results[phase] = seaweedfs.result.to_dict()
            if not dry_run:
                current_ledger = build_seaweedfs_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    service_resource_id=seaweedfs.service_resource_id,
                    data_resource_id=seaweedfs.data_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "nextcloud":
            nextcloud = reconcile_nextcloud(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.nextcloud,
            )
            phase_results[phase] = nextcloud.result.to_dict()
            if not dry_run:
                current_ledger = build_nextcloud_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    nextcloud_service_resource_id=nextcloud.nextcloud_service_resource_id,
                    onlyoffice_service_resource_id=nextcloud.onlyoffice_service_resource_id,
                    nextcloud_volume_resource_id=nextcloud.nextcloud_volume_resource_id,
                    onlyoffice_volume_resource_id=nextcloud.onlyoffice_volume_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "coder":
            coder = reconcile_coder(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.coder,
            )
            phase_results[phase] = coder.result.to_dict()
            if not dry_run:
                current_ledger = build_coder_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    service_resource_id=coder.service_resource_id,
                    data_resource_id=coder.data_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "openclaw":
            advisor = reconcile_openclaw(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.openclaw,
            )
            phase_results[phase] = advisor.result.to_dict()
            if not dry_run:
                current_ledger = build_openclaw_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    service_resource_id=advisor.service_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "my-farm-advisor":
            advisor = reconcile_my_farm_advisor(
                dry_run=dry_run,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.openclaw,
            )
            phase_results[phase] = advisor.result.to_dict()
            if not dry_run:
                current_ledger = build_my_farm_advisor_ledger(
                    existing_ledger=current_ledger,
                    stack_name=desired_state.stack_name,
                    service_resource_id=advisor.service_resource_id,
                )
                write_ownership_ledger(state_dir, current_ledger)
        elif phase == "cloudflare_access":
            access = reconcile_cloudflare_access(
                dry_run=dry_run,
                raw_env=raw_env,
                desired_state=desired_state,
                ownership_ledger=current_ledger,
                backend=backends.networking,
            )
            phase_results[phase] = access.result.to_dict()
            if not dry_run:
                current_ledger = build_access_ledger(
                    existing_ledger=current_ledger,
                    account_id=access.result.account_id,
                    provider_resource_id=access.provider_resource_id,
                    application_resource_ids=access.application_resource_ids,
                    policy_resource_ids=access.policy_resource_ids,
                )
                write_ownership_ledger(state_dir, current_ledger)
        if not dry_run:
            valid_phases.add(phase)
            _promote_preserved_phases(
                lifecycle_plan.preserved_phases, applicable_phases, valid_phases
            )
            _write_checkpoint(state_dir, desired_state, applicable_phases, valid_phases)

    return _build_summary(
        lifecycle_plan=lifecycle_plan,
        preflight_report=preflight_report,
        desired_state=desired_state,
        dry_run=dry_run,
        phase_results=phase_results,
        state_status="existing" if lifecycle_plan.mode != "install" else "fresh",
    )


def _write_checkpoint(
    state_dir: Path,
    desired_state: DesiredState,
    applicable_phases: tuple[str, ...],
    valid_phases: set[str],
) -> None:
    completed_steps = _longest_prefix(applicable_phases, valid_phases)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=desired_state.format_version,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=completed_steps,
            lifecycle_checkpoint_contract_version=LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
        ),
    )


def _longest_prefix(applicable_phases: tuple[str, ...], valid_phases: set[str]) -> tuple[str, ...]:
    prefix: list[str] = []
    for phase in applicable_phases:
        if phase not in valid_phases:
            break
        prefix.append(phase)
    return tuple(prefix)


def _promote_preserved_phases(
    preserved_phases: tuple[str, ...],
    applicable_phases: tuple[str, ...],
    valid_phases: set[str],
) -> None:
    for phase in applicable_phases:
        if phase in valid_phases:
            continue
        if phase not in preserved_phases:
            break
        valid_phases.add(phase)


def _emit_dokploy_ready_hint(url: str) -> None:
    print(
        f"Dokploy is ready: {url} — you can open it now and watch the rest of the install.",
        file=sys.stderr,
    )


def _build_summary(
    *,
    lifecycle_plan: LifecyclePlan,
    preflight_report: PreflightReport,
    desired_state: DesiredState,
    dry_run: bool,
    phase_results: dict[str, dict[str, Any]],
    state_status: str,
) -> dict[str, Any]:
    return {
        "bootstrap": phase_results.get("dokploy_bootstrap", {"outcome": "not_run"}),
        "desired_state": desired_state.to_dict(),
        "dry_run": dry_run,
        "headscale": phase_results.get("headscale", {"outcome": "not_run"}),
        "lifecycle": {
            "applicable_phases": list(lifecycle_plan.applicable_phases),
            "initial_completed_steps": list(lifecycle_plan.initial_completed_steps),
            "mode": lifecycle_plan.mode,
            "phases_to_run": list(lifecycle_plan.phases_to_run),
            "preserved_phases": list(lifecycle_plan.preserved_phases),
            "reasons": list(lifecycle_plan.reasons),
            "start_phase": lifecycle_plan.start_phase,
        },
        "matrix": phase_results.get("matrix", {"outcome": "not_run"}),
        "my_farm_advisor": phase_results.get("my-farm-advisor", {"outcome": "not_run"}),
        "nextcloud": phase_results.get("nextcloud", {"outcome": "not_run"}),
        "coder": phase_results.get("coder", {"outcome": "not_run"}),
        "networking": phase_results.get("networking", {"outcome": "not_run"}),
        "openclaw": phase_results.get("openclaw", {"outcome": "not_run"}),
        "preflight": preflight_report.to_dict(),
        "seaweedfs": phase_results.get("seaweedfs", {"outcome": "not_run"}),
        "shared_core": phase_results.get("shared_core", {"outcome": "not_run"}),
        "state_status": state_status,
        "tailscale": phase_results.get("tailscale", {"outcome": "not_run"}),
        "cloudflare_access": phase_results.get("cloudflare_access", {"outcome": "not_run"}),
    }


def _preserved_result(
    *,
    phase: str,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backends: LifecycleBackends,
) -> dict[str, Any]:
    if phase == "dokploy_bootstrap":
        return reconcile_dokploy(dry_run=True, backend=backends.bootstrap).to_dict()
    if phase == "tailscale":
        result = reconcile_tailscale(
            dry_run=True,
            raw_env=raw_env,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.tailscale,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "networking":
        result = reconcile_networking(
            dry_run=True,
            raw_env=raw_env,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.networking,
            connector_backend=backends.cloudflared,
        ).result.to_dict()
        result["outcome"] = "already_present"
        return result
    if phase == "shared_core":
        result = reconcile_shared_core(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.shared_core,
        ).result.to_dict()
        if result["outcome"] != "not_required":
            result["outcome"] = "already_present"
        return result
    if phase == "headscale":
        result = reconcile_headscale(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.headscale,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "matrix":
        result = reconcile_matrix(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.matrix,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "seaweedfs":
        result = reconcile_seaweedfs(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.seaweedfs,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "nextcloud":
        result = reconcile_nextcloud(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.nextcloud,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "coder":
        result = reconcile_coder(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.coder,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "openclaw":
        result = reconcile_openclaw(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.openclaw,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "my-farm-advisor":
        result = reconcile_my_farm_advisor(
            dry_run=True,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.openclaw,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    if phase == "cloudflare_access":
        result = reconcile_cloudflare_access(
            dry_run=True,
            raw_env=raw_env,
            desired_state=desired_state,
            ownership_ledger=ownership_ledger,
            backend=backends.networking,
        ).result.to_dict()
        if result["outcome"] != "skipped":
            result["outcome"] = "already_present"
        return result
    return {
        "notes": ["Phase preserved from an existing successful checkpoint."],
        "outcome": "not_run",
    }
