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
from dokploy_wizard.networking.planner import _build_tunnel_ingress
from dokploy_wizard.packs.coder import CoderBackend, CoderResourceRecord, reconcile_coder
from dokploy_wizard.packs.docuseal import (
    DocuSealBackend,
    DocuSealResourceRecord,
    reconcile_docuseal,
)
from dokploy_wizard.packs.headscale import (
    HeadscaleBackend,
    HeadscaleResourceRecord,
    reconcile_headscale,
)
from dokploy_wizard.packs.matrix import MatrixBackend, reconcile_matrix
from dokploy_wizard.packs.moodle import MoodleBackend, MoodleResourceRecord, reconcile_moodle
from dokploy_wizard.packs.nextcloud import (
    NextcloudBackend,
    NextcloudResourceRecord,
    reconcile_nextcloud,
)
from dokploy_wizard.packs.openclaw import (
    OpenClawBackend,
    OpenClawResourceRecord,
    reconcile_my_farm_advisor,
    reconcile_openclaw,
)
from dokploy_wizard.packs.seaweedfs import (
    SeaweedFsBackend,
    SeaweedFsResourceRecord,
    reconcile_seaweedfs,
)
from dokploy_wizard.state.models import DesiredState, OwnershipLedger, RawEnvInput
from dokploy_wizard.tailscale import TailscaleBackend, reconcile_tailscale


class LifecycleDriftError(RuntimeError):
    """Raised when preserved lifecycle phases drift from persisted ownership/state."""

    def __init__(self, message: str, *, report: "DriftReport") -> None:
        super().__init__(message)
        self.report = report


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
    coder_backend: CoderBackend,
    openclaw_backend: OpenClawBackend,
    moodle_backend: MoodleBackend | None = None,
    docuseal_backend: DocuSealBackend | None = None,
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
                moodle_backend=moodle_backend,
                docuseal_backend=docuseal_backend,
                seaweedfs_backend=seaweedfs_backend,
                coder_backend=coder_backend,
                openclaw_backend=openclaw_backend,
            )
        )
    report = DriftReport(entries=tuple(entries))
    if report.has_drift():
        details = "; ".join(
            f"{entry.phase}: {entry.detail}" for entry in report.entries if entry.status == "drift"
        )
        raise LifecycleDriftError(
            f"Lifecycle drift detected before mutation: {details}",
            report=report,
        )
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
    coder_backend: CoderBackend,
    openclaw_backend: OpenClawBackend,
    moodle_backend: MoodleBackend | None = None,
    docuseal_backend: DocuSealBackend | None = None,
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
            tunnel = networking_result.tunnel
            account_id = raw_env.values.get("CLOUDFLARE_ACCOUNT_ID")
            if not account_id:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        "Cloudflare account id is missing, so tunnel configuration "
                        "cannot be validated."
                    ),
                )
            get_tunnel_configuration = getattr(networking_backend, "get_tunnel_configuration", None)
            if (
                callable(get_tunnel_configuration)
                and raw_env.values.get("CLOUDFLARE_MOCK_ACCOUNT_OK") != "true"
            ):
                desired_ingress = _build_tunnel_ingress(desired_state)
                live_ingress = get_tunnel_configuration(account_id, tunnel.tunnel_id)
                if live_ingress != desired_ingress:
                    return DriftEntry(
                        phase=phase,
                        status="drift",
                        detail=(
                            "Cloudflare tunnel ingress no longer matches the desired "
                            "wizard configuration; "
                            "rerun the networking phase."
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
            if _is_repairable_legacy_cloudflare_access_gap(
                actions=actions,
                ownership_ledger=ownership_ledger,
            ):
                return DriftEntry(
                    phase=phase,
                    status="ok",
                    detail=(
                        "Cloudflare Access ownership uses the legacy no-ledger pattern; "
                        "allowing repairable create/reuse_existing rerun for this preserved phase."
                    ),
                )
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
                for resource in (
                    shared_core.network,
                    shared_core.postgres,
                    shared_core.redis,
                    shared_core.mail_relay,
                    shared_core.litellm,
                )
                if resource is not None
            }
            if actions - {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Shared core expected owned reuse, found actions {sorted(actions)}.",
                )
            validate_allocations = getattr(
                shared_core_backend, "validate_postgres_allocations", None
            )
            if callable(validate_allocations):
                postgres_allocations = tuple(
                    allocation
                    for allocation in (
                        desired_state.shared_core.litellm.postgres
                        if desired_state.shared_core.litellm is not None
                        else None,
                    )
                    if allocation is not None
                ) + tuple(
                    allocation.postgres
                    for allocation in desired_state.shared_core.allocations
                    if allocation.postgres is not None
                )
                if not validate_allocations(postgres_allocations):
                    return DriftEntry(
                        phase=phase,
                        status="drift",
                        detail=(
                            "Shared-core Postgres allocations are not ready for all preserved "
                            "packs; "
                            "rerun the phase."
                        ),
                    )
            validate_litellm_config = getattr(shared_core_backend, "validate_litellm_config", None)
            if callable(validate_litellm_config) and desired_state.shared_core.litellm is not None:
                if not validate_litellm_config(desired_state=desired_state):
                    return DriftEntry(
                        phase=phase,
                        status="drift",
                        detail=(
                            "LiteLLM config no longer matches the desired wizard inputs; "
                            "rerun the shared-core phase."
                        ),
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
            if headscale.health_check is None:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="Headscale health check metadata is missing for a preserved phase.",
                )
            if not headscale_backend.check_health(
                service=HeadscaleResourceRecord(
                    resource_id=headscale.service.resource_id,
                    resource_name=headscale.service.resource_name,
                ),
                url=headscale.health_check.url,
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        f"Headscale health check no longer passes for "
                        f"{headscale.health_check.url!r}."
                    ),
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
            if (
                nextcloud.nextcloud is None
                or nextcloud.onlyoffice is None
                or nextcloud.nextcloud.health_check is None
                or nextcloud.onlyoffice.health_check is None
                or not nextcloud_backend.check_health(
                    service=NextcloudResourceRecord(
                        resource_id=nextcloud.nextcloud.service.resource_id,
                        resource_name=nextcloud.nextcloud.service.resource_name,
                    ),
                    url=nextcloud.nextcloud.health_check.url,
                )
                or not nextcloud_backend.check_health(
                    service=NextcloudResourceRecord(
                        resource_id=nextcloud.onlyoffice.service.resource_id,
                        resource_name=nextcloud.onlyoffice.service.resource_name,
                    ),
                    url=nextcloud.onlyoffice.health_check.url,
                )
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        "Nextcloud or OnlyOffice is no longer healthy enough to "
                        "preserve; rerun the phase."
                    ),
                )
            return DriftEntry(
                phase=phase, status="ok", detail="Nextcloud ownership remains aligned."
            )
        if phase == "moodle":
            if moodle_backend is None:
                return DriftEntry(phase=phase, status="drift", detail="Moodle backend is unavailable.")
            moodle = reconcile_moodle(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=moodle_backend,
            ).result
            if moodle.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="Moodle remains skipped.")
            actions = {
                resource.action
                for resource in (moodle.service, moodle.persistent_data)
                if resource is not None
            }
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Moodle expected owned reuse, found actions {sorted(actions)}.",
                )
            if moodle.health_check is None or moodle.service is None or not moodle_backend.check_health(
                service=MoodleResourceRecord(
                    resource_id=moodle.service.resource_id,
                    resource_name=moodle.service.resource_name,
                ),
                url=moodle.health_check.url,
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="Moodle health check no longer passes for a preserved phase.",
                )
            return DriftEntry(phase=phase, status="ok", detail="Moodle ownership remains aligned.")
        if phase == "docuseal":
            if docuseal_backend is None:
                return DriftEntry(phase=phase, status="drift", detail="DocuSeal backend is unavailable.")
            docuseal = reconcile_docuseal(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=docuseal_backend,
            ).result
            if docuseal.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="DocuSeal remains skipped.")
            actions = {
                resource.action
                for resource in (docuseal.service, docuseal.persistent_data)
                if resource is not None
            }
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"DocuSeal expected owned reuse, found actions {sorted(actions)}.",
                )
            bootstrap_state = getattr(docuseal, "bootstrap_state", None)
            if bootstrap_state is not None and not bootstrap_state.initialized:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="DocuSeal bootstrap state is not initialized.",
                )
            if docuseal.health_state is None or docuseal.service is None or not docuseal_backend.check_health(
                service=DocuSealResourceRecord(
                    resource_id=docuseal.service.resource_id,
                    resource_name=docuseal.service.resource_name,
                ),
                url=docuseal.health_state.url,
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="DocuSeal health check no longer passes for a preserved phase.",
                )
            return DriftEntry(phase=phase, status="ok", detail="DocuSeal ownership remains aligned.")
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
            if seaweedfs.health_check is None:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="SeaweedFS health check metadata is missing for a preserved phase.",
                )
            if seaweedfs.service is None or not seaweedfs_backend.check_health(
                service=SeaweedFsResourceRecord(
                    resource_id=seaweedfs.service.resource_id,
                    resource_name=seaweedfs.service.resource_name,
                ),
                url=seaweedfs.health_check.url,
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        f"SeaweedFS health check no longer passes for "
                        f"{seaweedfs.health_check.url!r}."
                    ),
                )
            return DriftEntry(
                phase=phase, status="ok", detail="SeaweedFS ownership remains aligned."
            )
        if phase == "coder":
            coder = reconcile_coder(
                dry_run=True,
                desired_state=desired_state,
                ownership_ledger=ownership_ledger,
                backend=coder_backend,
            ).result
            if coder.outcome == "skipped":
                return DriftEntry(phase=phase, status="ok", detail="Coder remains skipped.")
            actions = {
                resource.action
                for resource in (coder.service, coder.persistent_data)
                if resource is not None
            }
            if actions != {"reuse_owned"}:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Coder expected owned reuse, found actions {sorted(actions)}.",
                )
            if coder.health_check is None:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="Coder health check metadata is missing for a preserved phase.",
                )
            if coder.service is None or not coder_backend.check_health(
                service=CoderResourceRecord(
                    resource_id=coder.service.resource_id,
                    resource_name=coder.service.resource_name,
                ),
                url=coder.health_check.url,
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=f"Coder health check no longer passes for {coder.health_check.url!r}.",
                )
            return DriftEntry(phase=phase, status="ok", detail="Coder ownership remains aligned.")
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
            if advisor.health_check is None:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail="OpenClaw health check metadata is missing for a preserved phase.",
                )
            if not openclaw_backend.check_health(
                service=OpenClawResourceRecord(
                    resource_id=advisor.service.resource_id,
                    resource_name=advisor.service.resource_name,
                    replicas=desired_state.openclaw_replicas or 1,
                ),
                url=advisor.health_check.url,
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        f"OpenClaw health check no longer passes for {advisor.health_check.url!r}."
                    ),
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
            if advisor.health_check is None:
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        "My Farm Advisor health check metadata is missing for a preserved phase."
                    ),
                )
            if not openclaw_backend.check_health(
                service=OpenClawResourceRecord(
                    resource_id=advisor.service.resource_id,
                    resource_name=advisor.service.resource_name,
                    replicas=desired_state.my_farm_advisor_replicas or 1,
                ),
                url=advisor.health_check.url,
            ):
                return DriftEntry(
                    phase=phase,
                    status="drift",
                    detail=(
                        f"My Farm Advisor health check no longer passes for "
                        f"{advisor.health_check.url!r}."
                    ),
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


def _is_repairable_legacy_cloudflare_access_gap(
    *, actions: set[str], ownership_ledger: OwnershipLedger
) -> bool:
    if actions != {"create", "reuse_existing"}:
        return False
    return not any(
        resource.resource_type.startswith("cloudflare_access_")
        for resource in ownership_ledger.resources
    )
