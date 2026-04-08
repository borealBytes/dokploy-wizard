"""Deterministic lifecycle drift normalization over the fixed phase order."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dokploy_wizard.bootstrap import DokployBootstrapBackend, reconcile_dokploy
from dokploy_wizard.core import SharedCoreBackend, reconcile_shared_core
from dokploy_wizard.lifecycle.changes import applicable_phases_for
from dokploy_wizard.networking import (
    CloudflareBackend,
    reconcile_cloudflare_access,
    reconcile_networking,
)
from dokploy_wizard.packs.headscale import HeadscaleBackend, reconcile_headscale
from dokploy_wizard.packs.matrix import MatrixBackend, reconcile_matrix
from dokploy_wizard.packs.nextcloud import NextcloudBackend, reconcile_nextcloud
from dokploy_wizard.packs.openclaw import (
    OpenClawBackend,
    reconcile_my_farm_advisor,
    reconcile_openclaw,
)
from dokploy_wizard.packs.seaweedfs import SeaweedFsBackend, reconcile_seaweedfs
from dokploy_wizard.state.models import DesiredState, OwnershipLedger, RawEnvInput
from dokploy_wizard.tailscale import TailscaleBackend, reconcile_tailscale


class LifecycleDriftError(RuntimeError):
    """Raised when preserved lifecycle phases drift from persisted ownership/state."""


@dataclass(frozen=True)
class DriftEntry:
    phase: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "phase": self.phase, "status": self.status}


@dataclass(frozen=True)
class DriftReport:
    entries: tuple[DriftEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {"entries": [entry.to_dict() for entry in self.entries]}

    def has_drift(self) -> bool:
        return any(entry.status == "drift" for entry in self.entries)


def validate_preserved_phases(
    *,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    preserved_phases: tuple[str, ...],
    bootstrap_backend: DokployBootstrapBackend,
    tailscale_backend: TailscaleBackend,
    networking_backend: CloudflareBackend,
    shared_core_backend: SharedCoreBackend,
    headscale_backend: HeadscaleBackend,
    matrix_backend: MatrixBackend,
    nextcloud_backend: NextcloudBackend,
    seaweedfs_backend: SeaweedFsBackend,
    openclaw_backend: OpenClawBackend,
) -> DriftReport:
    applicable = applicable_phases_for(desired_state)
    entries: list[DriftEntry] = []
    for phase in applicable:
        if phase not in preserved_phases:
            continue
        entries.append(
            _validate_phase(
                phase=phase,
                raw_env=raw_env,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                bootstrap_backend=bootstrap_backend,
                tailscale_backend=tailscale_backend,
                networking_backend=networking_backend,
                shared_core_backend=shared_core_backend,
                headscale_backend=headscale_backend,
                matrix_backend=matrix_backend,
                nextcloud_backend=nextcloud_backend,
                seaweedfs_backend=seaweedfs_backend,
                openclaw_backend=openclaw_backend,
            )
        )
    report = DriftReport(entries=tuple(entries))
    if report.has_drift():
        details = "; ".join(
            f"{entry.phase}: {entry.detail}" for entry in report.entries if entry.status == "drift"
        )
        raise LifecycleDriftError(f"Lifecycle drift detected before mutation: {details}")
    return report


def _validate_phase(
    *,
    phase: str,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    bootstrap_backend: DokployBootstrapBackend,
    tailscale_backend: TailscaleBackend,
    networking_backend: CloudflareBackend,
    shared_core_backend: SharedCoreBackend,
    headscale_backend: HeadscaleBackend,
    matrix_backend: MatrixBackend,
    nextcloud_backend: NextcloudBackend,
    seaweedfs_backend: SeaweedFsBackend,
    openclaw_backend: OpenClawBackend,
) -> DriftEntry:
    try:
        if phase == "preflight":
            return DriftEntry(phase=phase, status="ok", detail="Preflight is always revalidated.")
        if phase == "dokploy_bootstrap":
            bootstrap_result = reconcile_dokploy(dry_run=True, backend=bootstrap_backend)
            if bootstrap_result.outcome != "already_present":
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="Dokploy is no longer locally healthy for a preserved lifecycle phase.",
                )
            return DriftEntry(phase=phase, status="ok", detail="Dokploy bootstrap remains healthy.")
        if phase == "tailscale":
            tailscale = reconcile_tailscale(
                dry_run=True,
                raw_env=raw_env,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=tailscale_backend,
            ).result
            if tailscale.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="Tailscale remains skipped.")
            if tailscale.node is None or tailscale.node.action != "reuse_owned":
                action = None if tailscale.node is None else tailscale.node.action
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Tailscale expected owned reuse, found action {action!r}.",
                )
            return DriftEntry(
                phase=phase, status="ok", detail="Tailscale ownership remains aligned."
            )
        if phase == "networking":
            networking_result = reconcile_networking(
                dry_run=True,
                raw_env=raw_env,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=networking_backend,
            ).result
            actions = {
                networking_result.tunnel.action,
                *(record.action for record in networking_result.dns_records),
            }
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        f"Networking expected only owned reuse, found actions {sorted(actions)}."
                    ),
                )
            return DriftEntry(
                phase=phase, status="ok", detail="Networking ownership remains aligned."
            )
        if phase == "cloudflare_access":
            access = reconcile_cloudflare_access(
                dry_run=True,
                raw_env=raw_env,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=networking_backend,
            ).result
            if access.outcome == "skipped":
                return DriftEntry(
                    phase=phase, status="ok", detail="Cloudflare Access remains skipped."
                )
            actions = {
                *([access.otp_provider.action] if access.otp_provider is not None else []),
                *(item.action for item in access.applications),
                *(item.action for item in access.policies),
            }
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        f"Cloudflare Access expected owned reuse, found actions {sorted(actions)}."
                    ),
                )
            return DriftEntry(
                phase=phase,
                status="ok",
                detail="Cloudflare Access ownership remains aligned.",
            )
        if phase == "shared_core":
            shared_core = reconcile_shared_core(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=shared_core_backend,
            ).result
            if shared_core.outcome == "not_required":
                return DriftEntry(phase=phase, status="ok", detail="Shared core is not required.")
            actions = {
                resource.action
                for resource in (shared_core.network, shared_core.postgres, shared_core.redis)
                if resource is not None
            }
            if actions - {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Shared core expected owned reuse, found actions {sorted(actions)}.",
                )
            return DriftEntry(
                phase=phase, status="ok", detail="Shared core ownership remains aligned."
            )
        if phase == "headscale":
            headscale = reconcile_headscale(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=headscale_backend,
            ).result
            if headscale.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="Headscale remains skipped.")
            if headscale.service is None or headscale.service.action != "reuse_owned":
                action = None if headscale.service is None else headscale.service.action
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Headscale expected owned reuse, found action {action!r}.",
                )
            return DriftEntry(
                phase=phase, status="ok", detail="Headscale ownership remains aligned."
            )
        if phase == "matrix":
            matrix = reconcile_matrix(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=matrix_backend,
            ).result
            if matrix.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="Matrix remains skipped.")
            actions = {
                resource.action
                for resource in (matrix.service, matrix.persistent_data)
                if resource is not None
            }
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Matrix expected owned reuse, found actions {sorted(actions)}.",
                )
            return DriftEntry(phase=phase, status="ok", detail="Matrix ownership remains aligned.")
        if phase == "nextcloud":
            nextcloud = reconcile_nextcloud(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=nextcloud_backend,
            ).result
            if nextcloud.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="Nextcloud remains skipped.")
            actions = {resource["action"] for resource in _nextcloud_actions(nextcloud)}
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Nextcloud expected owned reuse, found actions {sorted(actions)}.",
                )
            return DriftEntry(
                phase=phase, status="ok", detail="Nextcloud ownership remains aligned."
            )
        if phase == "seaweedfs":
            seaweedfs = reconcile_seaweedfs(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=seaweedfs_backend,
            ).result
            if seaweedfs.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="SeaweedFS remains skipped.")
            actions = {
                resource.action
                for resource in (seaweedfs.service, seaweedfs.persistent_data)
                if resource is not None
            }
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"SeaweedFS expected owned reuse, found actions {sorted(actions)}.",
                )
            return DriftEntry(
                phase=phase, status="ok", detail="SeaweedFS ownership remains aligned."
            )
        if phase == "openclaw":
            advisor = reconcile_openclaw(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=openclaw_backend,
            ).result
            if advisor.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="OpenClaw remains skipped.")
            if advisor.service is None or advisor.service.action != "reuse_owned":
                action = None if advisor.service is None else advisor.service.action
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"OpenClaw expected owned reuse, found action {action!r}.",
                )
            return DriftEntry(
                phase=phase, status="ok", detail="OpenClaw ownership remains aligned."
            )
        if phase == "my-farm-advisor":
            advisor = reconcile_my_farm_advisor(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=openclaw_backend,
            ).result
            if advisor.outcome == "skipped":
                return DriftEntry(
                    phase=phase, status="ok", detail="My Farm Advisor remains skipped."
                )
            if advisor.service is None or advisor.service.action != "reuse_owned":
                action = None if advisor.service is None else advisor.service.action
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"My Farm Advisor expected owned reuse, found action {action!r}.",
                )
            return DriftEntry(
                phase=phase,
                status="ok",
                detail="My Farm Advisor ownership remains aligned.",
            )
    except RuntimeError as error:
        return DriftEntry(phase=phase, status="drift", detail=str(error))
    return DriftEntry(phase=phase, status="ok", detail=f"Phase '{phase}' validated.")


def _nextcloud_actions(result: Any) -> tuple[dict[str, str], ...]:
    nextcloud = result.nextcloud
    onlyoffice = result.onlyoffice
    resources: list[dict[str, str]] = []
    for service in (nextcloud, onlyoffice):
        if service is None:
            continue
        resources.append(service.service.to_dict())
        resources.append(service.data_volume.to_dict())
    return tuple(resources)
