"""Ledger-driven uninstall planning rules."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.core import (
    SHARED_LITELLM_RESOURCE_TYPE,
    SHARED_NETWORK_RESOURCE_TYPE,
    SHARED_POSTGRES_RESOURCE_TYPE,
    SHARED_REDIS_RESOURCE_TYPE,
)
from dokploy_wizard.lifecycle.changes import applicable_phases_for
from dokploy_wizard.networking import (
    ACCESS_APPLICATION_RESOURCE_TYPE,
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
    ACCESS_POLICY_RESOURCE_TYPE,
    DNS_RESOURCE_TYPE,
    TUNNEL_RESOURCE_TYPE,
)
from dokploy_wizard.packs.coder import CODER_DATA_RESOURCE_TYPE, CODER_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.docuseal import (
    DOCUSEAL_DATA_RESOURCE_TYPE,
    DOCUSEAL_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.headscale import HEADSCALE_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.matrix import MATRIX_DATA_RESOURCE_TYPE, MATRIX_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.moodle import MOODLE_DATA_RESOURCE_TYPE, MOODLE_SERVICE_RESOURCE_TYPE
from dokploy_wizard.packs.nextcloud import (
    NEXTCLOUD_SERVICE_RESOURCE_TYPE,
    NEXTCLOUD_VOLUME_RESOURCE_TYPE,
    ONLYOFFICE_SERVICE_RESOURCE_TYPE,
    ONLYOFFICE_VOLUME_RESOURCE_TYPE,
)
from dokploy_wizard.packs.openclaw import (
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
    OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
    OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
    OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
    OPENCLAW_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.packs.seaweedfs import (
    SEAWEEDFS_DATA_RESOURCE_TYPE,
    SEAWEEDFS_SERVICE_RESOURCE_TYPE,
)
from dokploy_wizard.state import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput
from dokploy_wizard.tailscale import TAILSCALE_NODE_RESOURCE_TYPE


class UninstallPlanningError(RuntimeError):
    """Raised when uninstall planning cannot safely classify ledger resources."""


@dataclass(frozen=True)
class DeletionRule:
    phase: str
    retain_safe: bool
    priority: int


@dataclass(frozen=True)
class PlannedDeletion:
    resource: OwnedResource
    phase: str
    policy: str

    def to_dict(self) -> dict[str, str]:
        return {
            "phase": self.phase,
            "policy": self.policy,
            "resource_id": self.resource.resource_id,
            "resource_type": self.resource.resource_type,
            "scope": self.resource.scope,
        }


@dataclass(frozen=True)
class UninstallPlan:
    mode: str
    environment: str
    deletions: tuple[PlannedDeletion, ...]
    retained_resources: tuple[OwnedResource, ...]
    warnings: tuple[str, ...]
    completed_steps_ceiling: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "deletions": [item.to_dict() for item in self.deletions],
            "environment": self.environment,
            "mode": self.mode,
            "retained_resources": [
                _resource_to_dict(resource) for resource in self.retained_resources
            ],
            "warnings": list(self.warnings),
        }


_RULES: dict[str, DeletionRule] = {
    TAILSCALE_NODE_RESOURCE_TYPE: DeletionRule(phase="tailscale", retain_safe=True, priority=5),
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE: DeletionRule(
        phase="cloudflare_access", retain_safe=True, priority=7
    ),
    ACCESS_APPLICATION_RESOURCE_TYPE: DeletionRule(
        phase="cloudflare_access", retain_safe=True, priority=8
    ),
    ACCESS_POLICY_RESOURCE_TYPE: DeletionRule(
        phase="cloudflare_access", retain_safe=True, priority=9
    ),
    OPENCLAW_SERVICE_RESOURCE_TYPE: DeletionRule(phase="openclaw", retain_safe=True, priority=10),
    OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE: DeletionRule(
        phase="openclaw", retain_safe=True, priority=11
    ),
    OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE: DeletionRule(
        phase="openclaw", retain_safe=True, priority=12
    ),
    OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE: DeletionRule(
        phase="openclaw", retain_safe=True, priority=13
    ),
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE: DeletionRule(
        phase="my-farm-advisor", retain_safe=True, priority=11
    ),
    NEXTCLOUD_SERVICE_RESOURCE_TYPE: DeletionRule(phase="nextcloud", retain_safe=True, priority=20),
    ONLYOFFICE_SERVICE_RESOURCE_TYPE: DeletionRule(
        phase="nextcloud", retain_safe=True, priority=21
    ),
    NEXTCLOUD_VOLUME_RESOURCE_TYPE: DeletionRule(phase="nextcloud", retain_safe=False, priority=22),
    ONLYOFFICE_VOLUME_RESOURCE_TYPE: DeletionRule(
        phase="nextcloud", retain_safe=False, priority=23
    ),
    MOODLE_SERVICE_RESOURCE_TYPE: DeletionRule(phase="moodle", retain_safe=True, priority=24),
    MOODLE_DATA_RESOURCE_TYPE: DeletionRule(phase="moodle", retain_safe=False, priority=25),
    DOCUSEAL_SERVICE_RESOURCE_TYPE: DeletionRule(
        phase="docuseal", retain_safe=True, priority=26
    ),
    DOCUSEAL_DATA_RESOURCE_TYPE: DeletionRule(phase="docuseal", retain_safe=False, priority=27),
    SEAWEEDFS_SERVICE_RESOURCE_TYPE: DeletionRule(phase="seaweedfs", retain_safe=True, priority=28),
    SEAWEEDFS_DATA_RESOURCE_TYPE: DeletionRule(phase="seaweedfs", retain_safe=False, priority=29),
    CODER_SERVICE_RESOURCE_TYPE: DeletionRule(phase="coder", retain_safe=True, priority=30),
    CODER_DATA_RESOURCE_TYPE: DeletionRule(phase="coder", retain_safe=False, priority=31),
    MATRIX_SERVICE_RESOURCE_TYPE: DeletionRule(phase="matrix", retain_safe=True, priority=30),
    MATRIX_DATA_RESOURCE_TYPE: DeletionRule(phase="matrix", retain_safe=False, priority=31),
    HEADSCALE_SERVICE_RESOURCE_TYPE: DeletionRule(phase="headscale", retain_safe=True, priority=40),
    SHARED_LITELLM_RESOURCE_TYPE: DeletionRule(phase="shared_core", retain_safe=True, priority=49),
    SHARED_NETWORK_RESOURCE_TYPE: DeletionRule(phase="shared_core", retain_safe=True, priority=50),
    SHARED_POSTGRES_RESOURCE_TYPE: DeletionRule(
        phase="shared_core", retain_safe=False, priority=51
    ),
    SHARED_REDIS_RESOURCE_TYPE: DeletionRule(phase="shared_core", retain_safe=False, priority=52),
    DNS_RESOURCE_TYPE: DeletionRule(phase="networking", retain_safe=True, priority=60),
    TUNNEL_RESOURCE_TYPE: DeletionRule(phase="networking", retain_safe=True, priority=61),
}

_PHASE_ORDER = {
    "tailscale": 0,
    "cloudflare_access": 1,
    "openclaw": 2,
    "my-farm-advisor": 3,
    "nextcloud": 4,
    "moodle": 5,
    "docuseal": 6,
    "seaweedfs": 7,
    "coder": 8,
    "matrix": 9,
    "headscale": 10,
    "shared_core": 11,
    "networking": 12,
}

_PACK_RUNTIME_RESOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "headscale": (HEADSCALE_SERVICE_RESOURCE_TYPE,),
    "matrix": (MATRIX_SERVICE_RESOURCE_TYPE,),
    "nextcloud": (NEXTCLOUD_SERVICE_RESOURCE_TYPE, ONLYOFFICE_SERVICE_RESOURCE_TYPE),
    "moodle": (MOODLE_SERVICE_RESOURCE_TYPE,),
    "docuseal": (DOCUSEAL_SERVICE_RESOURCE_TYPE,),
    "seaweedfs": (SEAWEEDFS_SERVICE_RESOURCE_TYPE,),
    "coder": (CODER_SERVICE_RESOURCE_TYPE,),
    "openclaw": (
        OPENCLAW_SERVICE_RESOURCE_TYPE,
        OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
        OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
        OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
        ACCESS_APPLICATION_RESOURCE_TYPE,
        ACCESS_POLICY_RESOURCE_TYPE,
    ),
    "my-farm-advisor": (
        MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
        ACCESS_APPLICATION_RESOURCE_TYPE,
        ACCESS_POLICY_RESOURCE_TYPE,
    ),
}

_PACK_DATA_RESOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "matrix": (MATRIX_DATA_RESOURCE_TYPE,),
    "nextcloud": (NEXTCLOUD_VOLUME_RESOURCE_TYPE, ONLYOFFICE_VOLUME_RESOURCE_TYPE),
    "moodle": (MOODLE_DATA_RESOURCE_TYPE,),
    "docuseal": (DOCUSEAL_DATA_RESOURCE_TYPE,),
    "seaweedfs": (SEAWEEDFS_DATA_RESOURCE_TYPE,),
    "coder": (CODER_DATA_RESOURCE_TYPE,),
    "openclaw": (),
    "my-farm-advisor": (),
    "headscale": (),
}

_PACK_HOSTNAME_KEYS: dict[str, tuple[str, ...]] = {
    "headscale": ("headscale",),
    "matrix": ("matrix",),
    "nextcloud": ("nextcloud", "onlyoffice"),
    "moodle": ("moodle",),
    "docuseal": ("docuseal",),
    "seaweedfs": ("s3",),
    "coder": ("coder",),
    "openclaw": ("openclaw",),
    "my-farm-advisor": ("my-farm-advisor",),
}


def build_uninstall_plan(
    *,
    raw_input: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    destroy_data: bool,
) -> UninstallPlan:
    del raw_input
    retained_resources: list[OwnedResource] = []
    deletions: list[PlannedDeletion] = []
    warnings: list[str] = []
    mode = "destroy" if destroy_data else "retain"

    for resource in ownership_ledger.resources:
        rule = _RULES.get(resource.resource_type)
        if rule is None:
            raise UninstallPlanningError(
                "Uninstall refuses to proceed because the ownership ledger contains "
                f"unsupported resource type '{resource.resource_type}'."
            )
        if destroy_data or rule.retain_safe:
            deletions.append(
                PlannedDeletion(
                    resource=resource,
                    phase=rule.phase,
                    policy="retain_safe" if rule.retain_safe else "destroy_only",
                )
            )
        else:
            retained_resources.append(resource)

    deletions.sort(
        key=lambda item: (
            _PHASE_ORDER[item.phase],
            _RULES[item.resource.resource_type].priority,
            item.resource.scope,
            item.resource.resource_id,
        )
    )
    retained_resources.sort(
        key=lambda resource: (resource.resource_type, resource.scope, resource.resource_id)
    )

    if retained_resources:
        warnings.append(
            "Retain-data uninstall will preserve wizard-owned data-bearing resources that are "
            "not proven runtime-safe from the current ledger semantics."
        )
    if destroy_data:
        warnings.append(
            "Destroy mode will delete all currently ledger-owned resources, including "
            "data-bearing services and volumes."
        )

    return UninstallPlan(
        mode=mode,
        environment=desired_state.stack_name,
        deletions=tuple(deletions),
        retained_resources=tuple(retained_resources),
        warnings=tuple(warnings),
        completed_steps_ceiling=None,
    )


def build_pack_disable_plan(
    *,
    existing_desired: DesiredState,
    requested_desired: DesiredState,
    ownership_ledger: OwnershipLedger,
) -> UninstallPlan:
    removed_packs = sorted(
        set(existing_desired.enabled_packs) - set(requested_desired.enabled_packs)
    )

    tailscale_disable_only = (
        existing_desired.enable_tailscale and not requested_desired.enable_tailscale
    )
    access_disable_only = (
        bool(existing_desired.cloudflare_access_otp_emails)
        and not bool(requested_desired.cloudflare_access_otp_emails)
    )
    if not removed_packs and not tailscale_disable_only and not access_disable_only:
        return UninstallPlan(
            mode="retain",
            environment=requested_desired.stack_name,
            deletions=(),
            retained_resources=(),
            warnings=(),
        )

    removable_types = {
        resource_type
        for pack_name in removed_packs
        for resource_type in _PACK_RUNTIME_RESOURCE_TYPES.get(pack_name, ())
    }
    retained_types = {
        resource_type
        for pack_name in removed_packs
        for resource_type in _PACK_DATA_RESOURCE_TYPES.get(pack_name, ())
    }
    removed_hostnames = {
        existing_desired.hostnames[key]
        for pack_name in removed_packs
        for key in _PACK_HOSTNAME_KEYS.get(pack_name, ())
        if key in existing_desired.hostnames
    }
    if (
        existing_desired.shared_core.requires_reconciliation()
        and not requested_desired.shared_core.requires_reconciliation()
    ):
        removable_types.add(SHARED_NETWORK_RESOURCE_TYPE)
        retained_types.update({SHARED_POSTGRES_RESOURCE_TYPE, SHARED_REDIS_RESOURCE_TYPE})
    if tailscale_disable_only:
        removable_types.add(TAILSCALE_NODE_RESOURCE_TYPE)
    if access_disable_only:
        removable_types.add(ACCESS_OTP_PROVIDER_RESOURCE_TYPE)

    deletions: list[PlannedDeletion] = []
    retained_resources: list[OwnedResource] = []
    for resource in ownership_ledger.resources:
        rule = _RULES.get(resource.resource_type)
        if rule is None:
            continue
        if resource.resource_type in retained_types:
            retained_resources.append(resource)
            continue
        if resource.resource_type in {
            ACCESS_APPLICATION_RESOURCE_TYPE,
            ACCESS_POLICY_RESOURCE_TYPE,
        }:
            if _access_scope_matches_any_hostname(resource.scope, removed_hostnames):
                deletions.append(
                    PlannedDeletion(
                        resource=resource,
                        phase=rule.phase,
                        policy="retain_safe" if rule.retain_safe else "destroy_only",
                    )
                )
            continue
        if resource.resource_type in removable_types:
            deletions.append(
                PlannedDeletion(
                    resource=resource,
                    phase=rule.phase,
                    policy="retain_safe" if rule.retain_safe else "destroy_only",
                )
            )
            continue
        if resource.resource_type == DNS_RESOURCE_TYPE and _dns_scope_matches_any_hostname(
            resource.scope, removed_hostnames
        ):
            deletions.append(
                PlannedDeletion(
                    resource=resource,
                    phase=rule.phase,
                    policy="retain_safe",
                )
            )

    deletions.sort(
        key=lambda item: (
            _PHASE_ORDER[item.phase],
            _RULES[item.resource.resource_type].priority,
            item.resource.scope,
            item.resource.resource_id,
        )
    )
    retained_resources.sort(
        key=lambda resource: (resource.resource_type, resource.scope, resource.resource_id)
    )

    warnings: list[str] = []
    if retained_resources:
        warnings.append(
            "Pack disable retains wizard-owned data resources when delete semantics are not "
            "proven safe by the current ownership-ledger model."
        )
    completed_steps_ceiling = _completed_steps_ceiling_for_pack_disable(
        existing_desired=existing_desired,
        requested_desired=requested_desired,
        removed_packs=removed_packs,
    )

    return UninstallPlan(
        mode="retain",
        environment=requested_desired.stack_name,
        deletions=tuple(deletions),
        retained_resources=tuple(retained_resources),
        warnings=tuple(warnings),
        completed_steps_ceiling=completed_steps_ceiling,
    )


def compute_remaining_completed_steps(
    *,
    desired_state: DesiredState,
    raw_input: RawEnvInput,
    ownership_ledger: OwnershipLedger,
) -> tuple[str, ...]:
    del raw_input
    resources_by_type: dict[str, list[OwnedResource]] = {}
    for resource in ownership_ledger.resources:
        resources_by_type.setdefault(resource.resource_type, []).append(resource)

    prefix = ["preflight", "dokploy_bootstrap"]
    if desired_state.enable_tailscale:
        if not _has_resource_type(resources_by_type, TAILSCALE_NODE_RESOURCE_TYPE):
            return tuple(prefix)
        prefix.append("tailscale")
    if not _networking_complete(desired_state, resources_by_type):
        return tuple(prefix)
    prefix.append("networking")
    if _cloudflare_access_complete(desired_state, resources_by_type):
        prefix.append("cloudflare_access")
    elif desired_state.cloudflare_access_otp_emails and {
        "openclaw",
        "my-farm-advisor",
    } & set(desired_state.enabled_packs):
        return tuple(prefix)
    if not _shared_core_complete(desired_state, resources_by_type):
        return tuple(prefix)
    prefix.append("shared_core")
    if not _headscale_complete(desired_state, resources_by_type):
        return tuple(prefix)
    prefix.append("headscale")
    if "matrix" in desired_state.enabled_packs:
        if not _has_resource_type(resources_by_type, MATRIX_SERVICE_RESOURCE_TYPE):
            return tuple(prefix)
        if not _has_resource_type(resources_by_type, MATRIX_DATA_RESOURCE_TYPE):
            return tuple(prefix)
        prefix.append("matrix")
    if "nextcloud" in desired_state.enabled_packs:
        for resource_type in (
            NEXTCLOUD_SERVICE_RESOURCE_TYPE,
            ONLYOFFICE_SERVICE_RESOURCE_TYPE,
            NEXTCLOUD_VOLUME_RESOURCE_TYPE,
            ONLYOFFICE_VOLUME_RESOURCE_TYPE,
        ):
            if not _has_resource_type(resources_by_type, resource_type):
                return tuple(prefix)
        prefix.append("nextcloud")
    if "moodle" in desired_state.enabled_packs:
        if not _has_resource_type(resources_by_type, MOODLE_SERVICE_RESOURCE_TYPE):
            return tuple(prefix)
        if not _has_resource_type(resources_by_type, MOODLE_DATA_RESOURCE_TYPE):
            return tuple(prefix)
        prefix.append("moodle")
    if "docuseal" in desired_state.enabled_packs:
        if not _has_resource_type(resources_by_type, DOCUSEAL_SERVICE_RESOURCE_TYPE):
            return tuple(prefix)
        if not _has_resource_type(resources_by_type, DOCUSEAL_DATA_RESOURCE_TYPE):
            return tuple(prefix)
        prefix.append("docuseal")
    if "seaweedfs" in desired_state.enabled_packs:
        if not _has_resource_type(resources_by_type, SEAWEEDFS_SERVICE_RESOURCE_TYPE):
            return tuple(prefix)
        if not _has_resource_type(resources_by_type, SEAWEEDFS_DATA_RESOURCE_TYPE):
            return tuple(prefix)
        prefix.append("seaweedfs")
    if "openclaw" in desired_state.enabled_packs:
        for resource_type in (
            OPENCLAW_SERVICE_RESOURCE_TYPE,
            OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE,
            OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE,
            OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE,
        ):
            if not _has_resource_type(resources_by_type, resource_type):
                return tuple(prefix)
        prefix.append("openclaw")
    if "my-farm-advisor" in desired_state.enabled_packs:
        if not _has_resource_type(resources_by_type, MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE):
            return tuple(prefix)
        prefix.append("my-farm-advisor")
    return tuple(prefix)


def _networking_complete(
    desired_state: DesiredState, resources_by_type: dict[str, list[OwnedResource]]
) -> bool:
    if not _has_resource_type(resources_by_type, TUNNEL_RESOURCE_TYPE):
        return False
    return len(resources_by_type.get(DNS_RESOURCE_TYPE, ())) == len(desired_state.hostnames)


def _shared_core_complete(
    desired_state: DesiredState, resources_by_type: dict[str, list[OwnedResource]]
) -> bool:
    if not desired_state.shared_core.requires_reconciliation():
        return True
    if not _has_resource_type(resources_by_type, SHARED_NETWORK_RESOURCE_TYPE):
        return False
    if desired_state.shared_core.postgres is not None and not _has_resource_type(
        resources_by_type, SHARED_POSTGRES_RESOURCE_TYPE
    ):
        return False
    if desired_state.shared_core.redis is not None and not _has_resource_type(
        resources_by_type, SHARED_REDIS_RESOURCE_TYPE
    ):
        return False
    if desired_state.shared_core.litellm is not None and not _has_resource_type(
        resources_by_type, SHARED_LITELLM_RESOURCE_TYPE
    ):
        return False
    return True


def _cloudflare_access_complete(
    desired_state: DesiredState, resources_by_type: dict[str, list[OwnedResource]]
) -> bool:
    if not desired_state.cloudflare_access_otp_emails:
        return True
    if not ({"openclaw", "my-farm-advisor"} & set(desired_state.enabled_packs)):
        return True
    if not _has_resource_type(resources_by_type, ACCESS_OTP_PROVIDER_RESOURCE_TYPE):
        return False
    advisor_hosts = [
        key
        for key in ("openclaw", "my-farm-advisor")
        if key in desired_state.enabled_packs and key in desired_state.hostnames
    ]
    return len(resources_by_type.get(ACCESS_APPLICATION_RESOURCE_TYPE, ())) >= len(
        advisor_hosts
    ) and len(resources_by_type.get(ACCESS_POLICY_RESOURCE_TYPE, ())) >= len(advisor_hosts)


def _headscale_complete(
    desired_state: DesiredState, resources_by_type: dict[str, list[OwnedResource]]
) -> bool:
    if "headscale" not in desired_state.enabled_packs:
        return True
    return _has_resource_type(resources_by_type, HEADSCALE_SERVICE_RESOURCE_TYPE)


def _has_resource_type(
    resources_by_type: dict[str, list[OwnedResource]], resource_type: str
) -> bool:
    return bool(resources_by_type.get(resource_type))


def _resource_to_dict(resource: OwnedResource) -> dict[str, str]:
    return {
        "resource_id": resource.resource_id,
        "resource_type": resource.resource_type,
        "scope": resource.scope,
    }


def _dns_scope_matches_any_hostname(scope: str, hostnames: set[str]) -> bool:
    return any(scope.endswith(f":{hostname}") for hostname in hostnames)


def _access_scope_matches_any_hostname(scope: str, hostnames: set[str]) -> bool:
    return any(scope.endswith(f":{hostname.lower()}") for hostname in hostnames)


def _completed_steps_ceiling_for_pack_disable(
    *,
    existing_desired: DesiredState,
    requested_desired: DesiredState,
    removed_packs: list[str],
) -> tuple[str, ...] | None:
    if not _shared_core_dirty_after_pack_disable(
        existing_desired=existing_desired,
        requested_desired=requested_desired,
        removed_packs=removed_packs,
    ):
        return None
    applicable_phases = applicable_phases_for(requested_desired)
    if "shared_core" not in applicable_phases:
        return None
    return applicable_phases[: applicable_phases.index("shared_core")]


def _shared_core_dirty_after_pack_disable(
    *,
    existing_desired: DesiredState,
    requested_desired: DesiredState,
    removed_packs: list[str],
) -> bool:
    if existing_desired.shared_core.to_dict() != requested_desired.shared_core.to_dict():
        return True
    return bool({"openclaw", "my-farm-advisor"} & set(removed_packs))
