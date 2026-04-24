"""Typed models for wizard state documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from dokploy_wizard.core.models import SharedCorePlan

STATE_FORMAT_VERSION = 1
LIFECYCLE_CHECKPOINT_CONTRACT_VERSION = 2


class StateValidationError(ValueError):
    """Raised when state documents fail validation."""


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        msg = f"Expected non-empty string for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        msg = f"Expected integer for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        msg = f"Expected boolean for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item == "" for item in value
    ):
        msg = f"Expected list of non-empty strings for '{key}'."
        raise StateValidationError(msg)
    return tuple(value)


def _require_string_map(payload: dict[str, Any], key: str) -> dict[str, str]:
    value = payload.get(key)
    if not isinstance(value, dict):
        msg = f"Expected object for '{key}'."
        raise StateValidationError(msg)

    normalized: dict[str, str] = {}
    for map_key, map_value in value.items():
        if not isinstance(map_key, str) or map_key == "":
            msg = f"Expected non-empty string keys in '{key}'."
            raise StateValidationError(msg)
        if not isinstance(map_value, str) or map_value == "":
            msg = f"Expected non-empty string values in '{key}'."
            raise StateValidationError(msg)
        normalized[map_key] = map_value
    return normalized


def _require_format_version(payload: dict[str, Any]) -> int:
    version = _require_int(payload, "format_version")
    if version != STATE_FORMAT_VERSION:
        msg = f"Unsupported format_version {version}; expected {STATE_FORMAT_VERSION}."
        raise StateValidationError(msg)
    return version


@dataclass(frozen=True)
class RawEnvInput:
    """Normalized raw env-file input."""

    format_version: int
    values: dict[str, str]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        if not self.values:
            msg = "Raw env input cannot be empty."
            raise StateValidationError(msg)
        for key, value in self.values.items():
            if key == "" or value == "":
                msg = "Raw env input keys and values must be non-empty strings."
                raise StateValidationError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "values": dict(sorted(self.values.items())),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RawEnvInput:
        return cls(
            format_version=_require_format_version(payload),
            values=_require_string_map(payload, "values"),
        )


@dataclass(frozen=True)
class DesiredState:
    """Deterministic desired configuration resolved from raw input."""

    format_version: int
    stack_name: str
    root_domain: str
    dokploy_url: str
    dokploy_api_url: str | None
    enable_tailscale: bool
    tailscale_hostname: str | None
    tailscale_enable_ssh: bool
    tailscale_tags: tuple[str, ...]
    tailscale_subnet_routes: tuple[str, ...]
    cloudflare_access_otp_emails: tuple[str, ...]
    enabled_features: tuple[str, ...]
    selected_packs: tuple[str, ...]
    enabled_packs: tuple[str, ...]
    hostnames: dict[str, str]
    seaweedfs_access_key: str | None
    seaweedfs_secret_key: str | None
    openclaw_gateway_token: str | None
    openclaw_channels: tuple[str, ...]
    openclaw_replicas: int | None
    my_farm_advisor_channels: tuple[str, ...]
    my_farm_advisor_replicas: int | None
    shared_core: SharedCorePlan
    access_wrapped_hostnames: dict[str, str] = field(default_factory=dict)
    machine_hostnames: dict[str, str] = field(default_factory=dict)
    workspace_daemon_packs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        if self.stack_name == "" or self.root_domain == "" or self.dokploy_url == "":
            msg = "Desired state core string fields must be non-empty."
            raise StateValidationError(msg)
        if self.dokploy_api_url == "":
            msg = "Desired state Dokploy API URL cannot be an empty string."
            raise StateValidationError(msg)
        if self.enable_tailscale and self.tailscale_hostname is None:
            msg = "Desired state tailscale hostname is required when Tailscale is enabled."
            raise StateValidationError(msg)
        if not self.enable_tailscale and self.tailscale_hostname is not None:
            msg = "Desired state tailscale hostname must be omitted when Tailscale is disabled."
            raise StateValidationError(msg)
        if sorted(self.tailscale_tags) != list(self.tailscale_tags):
            msg = "Tailscale tags must be stored in sorted order."
            raise StateValidationError(msg)
        if sorted(self.tailscale_subnet_routes) != list(self.tailscale_subnet_routes):
            msg = "Tailscale subnet routes must be stored in sorted order."
            raise StateValidationError(msg)
        if sorted(self.cloudflare_access_otp_emails) != list(self.cloudflare_access_otp_emails):
            msg = "Cloudflare Access OTP emails must be stored in sorted order."
            raise StateValidationError(msg)
        if (self.seaweedfs_access_key is None) != (self.seaweedfs_secret_key is None):
            msg = "SeaweedFS access and secret keys must be provided together."
            raise StateValidationError(msg)
        if "seaweedfs" in self.enabled_packs and self.seaweedfs_access_key is None:
            msg = "SeaweedFS access/secret keys are required when the SeaweedFS pack is enabled."
            raise StateValidationError(msg)
        if "seaweedfs" not in self.enabled_packs and self.seaweedfs_access_key is not None:
            msg = "SeaweedFS credentials must be omitted when the SeaweedFS pack is disabled."
            raise StateValidationError(msg)
        if sorted(self.enabled_features) != list(self.enabled_features):
            msg = "Enabled features must be stored in sorted order."
            raise StateValidationError(msg)
        if sorted(self.selected_packs) != list(self.selected_packs):
            msg = "Selected packs must be stored in sorted order."
            raise StateValidationError(msg)
        if sorted(self.enabled_packs) != list(self.enabled_packs):
            msg = "Enabled packs must be stored in sorted order."
            raise StateValidationError(msg)
        if not set(self.selected_packs).issubset(self.enabled_packs):
            msg = "Selected packs must be a subset of enabled packs."
            raise StateValidationError(msg)
        if sorted(self.openclaw_channels) != list(self.openclaw_channels):
            msg = "OpenClaw channels must be stored in sorted order."
            raise StateValidationError(msg)
        if "openclaw" not in self.enabled_packs and self.openclaw_gateway_token is not None:
            msg = "OpenClaw gateway token must be omitted when the OpenClaw pack is disabled."
            raise StateValidationError(msg)
        if sorted(self.my_farm_advisor_channels) != list(self.my_farm_advisor_channels):
            msg = "My Farm Advisor channels must be stored in sorted order."
            raise StateValidationError(msg)
        if self.openclaw_replicas is not None and self.openclaw_replicas < 1:
            msg = "OpenClaw replicas must be a positive integer when configured."
            raise StateValidationError(msg)
        if self.my_farm_advisor_replicas is not None and self.my_farm_advisor_replicas < 1:
            msg = "My Farm Advisor replicas must be a positive integer when configured."
            raise StateValidationError(msg)
        if any(value == "" for value in self.hostnames.values()):
            msg = "Desired hostnames must be non-empty strings."
            raise StateValidationError(msg)
        if any(value == "" for value in self.access_wrapped_hostnames.values()):
            msg = "Desired access-wrapped hostnames must be non-empty strings."
            raise StateValidationError(msg)
        if any(value == "" for value in self.machine_hostnames.values()):
            msg = "Desired machine hostnames must be non-empty strings."
            raise StateValidationError(msg)
        if sorted(self.workspace_daemon_packs) != list(self.workspace_daemon_packs):
            msg = "Workspace daemon packs must be stored in sorted order."
            raise StateValidationError(msg)
        if not set(self.access_wrapped_hostnames).issubset(self.hostnames):
            msg = "Access-wrapped hostnames must reference known hostnames."
            raise StateValidationError(msg)
        if not set(self.machine_hostnames).issubset(self.hostnames):
            msg = "Machine hostnames must reference known hostnames."
            raise StateValidationError(msg)
        if not set(self.workspace_daemon_packs).issubset(self.enabled_packs):
            msg = "Workspace daemon packs must be enabled packs."
            raise StateValidationError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "stack_name": self.stack_name,
            "root_domain": self.root_domain,
            "dokploy_url": self.dokploy_url,
            "dokploy_api_url": self.dokploy_api_url,
            "enable_tailscale": self.enable_tailscale,
            "tailscale_hostname": self.tailscale_hostname,
            "tailscale_enable_ssh": self.tailscale_enable_ssh,
            "tailscale_tags": list(self.tailscale_tags),
            "tailscale_subnet_routes": list(self.tailscale_subnet_routes),
            "cloudflare_access_otp_emails": list(self.cloudflare_access_otp_emails),
            "enabled_features": list(self.enabled_features),
            "selected_packs": list(self.selected_packs),
            "enabled_packs": list(self.enabled_packs),
            "hostnames": dict(sorted(self.hostnames.items())),
            "access_wrapped_hostnames": dict(sorted(self.access_wrapped_hostnames.items())),
            "machine_hostnames": dict(sorted(self.machine_hostnames.items())),
            "workspace_daemon_packs": list(self.workspace_daemon_packs),
            "seaweedfs_access_key": self.seaweedfs_access_key,
            "seaweedfs_secret_key": self.seaweedfs_secret_key,
            "openclaw_gateway_token": self.openclaw_gateway_token,
            "openclaw_channels": list(self.openclaw_channels),
            "openclaw_replicas": self.openclaw_replicas,
            "my_farm_advisor_channels": list(self.my_farm_advisor_channels),
            "my_farm_advisor_replicas": self.my_farm_advisor_replicas,
            "shared_core": self.shared_core.to_dict(),
        }

    def fingerprint(self) -> str:
        import json

        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DesiredState:
        return cls(
            format_version=_require_format_version(payload),
            stack_name=_require_string(payload, "stack_name"),
            root_domain=_require_string(payload, "root_domain"),
            dokploy_url=_require_string(payload, "dokploy_url"),
            dokploy_api_url=_require_optional_string(payload, "dokploy_api_url"),
            enable_tailscale=_require_bool(payload, "enable_tailscale"),
            tailscale_hostname=_require_optional_string(payload, "tailscale_hostname"),
            tailscale_enable_ssh=_require_bool(payload, "tailscale_enable_ssh"),
            tailscale_tags=_require_string_list(payload, "tailscale_tags"),
            tailscale_subnet_routes=_require_string_list(payload, "tailscale_subnet_routes"),
            cloudflare_access_otp_emails=_require_string_list(
                payload, "cloudflare_access_otp_emails"
            ),
            enabled_features=_require_string_list(payload, "enabled_features"),
            selected_packs=_require_string_list(payload, "selected_packs"),
            enabled_packs=_require_string_list(payload, "enabled_packs"),
            hostnames=_require_string_map(payload, "hostnames"),
            access_wrapped_hostnames=_require_optional_string_map(
                payload, "access_wrapped_hostnames"
            ),
            machine_hostnames=_require_optional_string_map(payload, "machine_hostnames"),
            workspace_daemon_packs=_require_optional_string_list(payload, "workspace_daemon_packs"),
            seaweedfs_access_key=_require_optional_string(payload, "seaweedfs_access_key"),
            seaweedfs_secret_key=_require_optional_string(payload, "seaweedfs_secret_key"),
            openclaw_gateway_token=_require_optional_string(payload, "openclaw_gateway_token"),
            openclaw_channels=_require_string_list(payload, "openclaw_channels"),
            openclaw_replicas=_require_optional_positive_int(payload, "openclaw_replicas"),
            my_farm_advisor_channels=_require_string_list(payload, "my_farm_advisor_channels"),
            my_farm_advisor_replicas=_require_optional_positive_int(
                payload, "my_farm_advisor_replicas"
            ),
            shared_core=_require_shared_core(payload, "shared_core"),
        )


def _require_shared_core(payload: dict[str, Any], key: str) -> SharedCorePlan:
    value = payload.get(key)
    if not isinstance(value, dict):
        msg = f"Expected object for '{key}'."
        raise StateValidationError(msg)
    try:
        return SharedCorePlan.from_dict(value)
    except ValueError as error:
        raise StateValidationError(str(error)) from error


def _require_optional_positive_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or value < 1:
        msg = f"Expected positive integer or null for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_positive_int_with_default(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or value < 1:
        msg = f"Expected positive integer for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"Expected string or null for '{key}'."
        raise StateValidationError(msg)
    return value


def _require_optional_string_map(payload: dict[str, Any], key: str) -> dict[str, str]:
    value = payload.get(key)
    if value is None:
        return {}
    return _require_string_map(payload, key)


def _require_optional_string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    return _require_string_list(payload, key)


@dataclass(frozen=True)
class AppliedStateCheckpoint:
    """Last known successfully applied state."""

    format_version: int
    desired_state_fingerprint: str
    completed_steps: tuple[str, ...]
    lifecycle_checkpoint_contract_version: int = LIFECYCLE_CHECKPOINT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)
        if self.desired_state_fingerprint == "":
            msg = "Applied state fingerprint cannot be empty."
            raise StateValidationError(msg)
        if any(step == "" for step in self.completed_steps):
            msg = "Applied state steps must be non-empty strings."
            raise StateValidationError(msg)
        if self.lifecycle_checkpoint_contract_version < 1:
            msg = "Lifecycle checkpoint contract version must be a positive integer."
            raise StateValidationError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "desired_state_fingerprint": self.desired_state_fingerprint,
            "completed_steps": list(self.completed_steps),
            "lifecycle_checkpoint_contract_version": self.lifecycle_checkpoint_contract_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppliedStateCheckpoint:
        return cls(
            format_version=_require_format_version(payload),
            desired_state_fingerprint=_require_string(payload, "desired_state_fingerprint"),
            completed_steps=_require_string_list(payload, "completed_steps"),
            lifecycle_checkpoint_contract_version=_require_positive_int_with_default(
                payload,
                "lifecycle_checkpoint_contract_version",
                default=1,
            ),
        )


@dataclass(frozen=True)
class OwnedResource:
    """Single resource tracked in the ownership ledger."""

    resource_type: str
    resource_id: str
    scope: str

    def __post_init__(self) -> None:
        if self.resource_type == "" or self.resource_id == "" or self.scope == "":
            msg = "Owned resources require non-empty type, id, and scope."
            raise StateValidationError(msg)

    def to_dict(self) -> dict[str, str]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OwnedResource:
        return cls(
            resource_type=_require_string(payload, "resource_type"),
            resource_id=_require_string(payload, "resource_id"),
            scope=_require_string(payload, "scope"),
        )


@dataclass(frozen=True)
class OwnershipLedger:
    """Future uninstall authority for wizard-owned resources."""

    format_version: int
    resources: tuple[OwnedResource, ...]

    def __post_init__(self) -> None:
        if self.format_version != STATE_FORMAT_VERSION:
            msg = (
                f"Unsupported format_version {self.format_version}; "
                f"expected {STATE_FORMAT_VERSION}."
            )
            raise StateValidationError(msg)

        seen: set[tuple[str, str]] = set()
        for resource in self.resources:
            resource_key = (resource.resource_type, resource.resource_id)
            if resource_key in seen:
                msg = (
                    "Ownership ledger contains duplicate resource identity "
                    f"{resource.resource_type}:{resource.resource_id}."
                )
                raise StateValidationError(msg)
            seen.add(resource_key)

    def to_dict(self) -> dict[str, Any]:
        ordered_resources = sorted(
            self.resources,
            key=lambda resource: (resource.resource_type, resource.resource_id, resource.scope),
        )
        return {
            "format_version": self.format_version,
            "resources": [resource.to_dict() for resource in ordered_resources],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OwnershipLedger:
        format_version = _require_format_version(payload)
        resources_payload = payload.get("resources")
        if not isinstance(resources_payload, list):
            msg = "Expected list for 'resources'."
            raise StateValidationError(msg)
        resources = tuple(
            OwnedResource.from_dict(item) for item in resources_payload if isinstance(item, dict)
        )
        if len(resources) != len(resources_payload):
            msg = "Each ownership ledger resource must be an object."
            raise StateValidationError(msg)
        return cls(format_version=format_version, resources=resources)
