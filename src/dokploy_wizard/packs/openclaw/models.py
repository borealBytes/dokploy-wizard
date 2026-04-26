"""Typed models for the OpenClaw/My Farm runtime reconciliation phase."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpenClawManagedResource:
    action: str
    resource_id: str
    resource_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
        }


@dataclass(frozen=True)
class OpenClawHealthCheck:
    url: str
    passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "url": self.url}


@dataclass(frozen=True)
class OpenClawNexaDeploymentContract:
    enabled: bool
    deployment_mode: str
    topology_mode: str
    mem0_mode: str
    credential_mediation_mode: str
    runtime_contract_path: str
    runtime_service_name: str | None
    runtime_state_dir: str
    workspace_root: str
    workspace_contract_path: str
    internal_network_only: bool
    mem0_service_name: str | None
    mem0_base_url: str | None
    qdrant_service_name: str | None
    qdrant_base_url: str | None
    secret_env_keys: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_mediation_mode": self.credential_mediation_mode,
            "deployment_mode": self.deployment_mode,
            "enabled": self.enabled,
            "internal_network_only": self.internal_network_only,
            "mem0_mode": self.mem0_mode,
            "mem0_base_url": self.mem0_base_url,
            "mem0_service_name": self.mem0_service_name,
            "notes": list(self.notes),
            "qdrant_base_url": self.qdrant_base_url,
            "qdrant_service_name": self.qdrant_service_name,
            "runtime_contract_path": self.runtime_contract_path,
            "runtime_service_name": self.runtime_service_name,
            "runtime_state_dir": self.runtime_state_dir,
            "secret_env_keys": list(self.secret_env_keys),
            "topology_mode": self.topology_mode,
            "workspace_contract_path": self.workspace_contract_path,
            "workspace_root": self.workspace_root,
        }


@dataclass(frozen=True)
class OpenClawResult:
    outcome: str
    enabled: bool
    variant: str | None
    hostname: str | None
    channels: tuple[str, ...]
    replicas: int | None
    template_path: str | None
    service: OpenClawManagedResource | None
    secret_refs: tuple[str, ...]
    health_check: OpenClawHealthCheck | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "channels": list(self.channels),
            "enabled": self.enabled,
            "health_check": None if self.health_check is None else self.health_check.to_dict(),
            "hostname": self.hostname,
            "notes": list(self.notes),
            "outcome": self.outcome,
            "replicas": self.replicas,
            "secret_refs": list(self.secret_refs),
            "service": None if self.service is None else self.service.to_dict(),
            "template_path": self.template_path,
            "variant": self.variant,
        }


@dataclass(frozen=True)
class OpenClawPhase:
    result: OpenClawResult
    service_resource_id: str | None


@dataclass(frozen=True)
class OpenClawResourceRecord:
    resource_id: str
    resource_name: str
    replicas: int
