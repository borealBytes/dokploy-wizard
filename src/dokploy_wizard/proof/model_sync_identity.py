"""Typed host identity and preflight evidence for model-sync proofs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Final, Literal, assert_never

from dokploy_wizard.proof import HostIdentityMode
from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    require_keys,
    require_list,
    require_mapping,
    require_text,
)
from dokploy_wizard.proof.model_sync_env import ProofNamespace

PreflightRole = Literal["host_a", "host_b"]
_SUPPORTED_ARCHITECTURES: Final = frozenset({"amd64", "arm64"})
_BOOT_ID: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class RemoteProofError(RuntimeError):
    """Raised when remote proof evidence violates its typed contract."""


@dataclass(frozen=True, slots=True)
class ObservedResource:
    resource_id: str
    name: str
    kind: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.resource_id, "kind": self.kind, "name": self.name}


@dataclass(frozen=True, slots=True)
class RemoteProbe:
    machine_sha256: str
    ssh_sha256: str
    boot_sha256: str
    architecture: str
    namespace_clean: bool
    inventory: dict[str, tuple[ObservedResource, ...]]
    plane_states: dict[str, str]
    plane_provenance: dict[str, str] = field(
        default_factory=lambda: {"docker": "docker_available_inventory"}
    )

    def to_dict(self) -> dict[str, JsonValue]:
        if "docker" not in self.plane_provenance:
            raise RemoteProofError("docker plane provenance is required")
        inventory: dict[str, JsonValue] = {
            key: {
                "resources": [resource.to_dict() for resource in value],
                "state": self.plane_states[key],
            }
            for key, value in sorted(self.inventory.items())
        }
        docker = require_mapping(inventory["docker"], "docker plane inventory")
        docker["provenance"] = self.plane_provenance["docker"]
        return {
            "architecture": self.architecture,
            "boot_sha256": self.boot_sha256,
            "inventory": inventory,
            "machine_sha256": self.machine_sha256,
            "namespace_clean": self.namespace_clean,
            "ssh_sha256": self.ssh_sha256,
        }


@dataclass(frozen=True, slots=True)
class HostInputNames:
    host_a: str
    password_a: str
    host_b: str
    password_b: str


def resolve_host_inputs(
    names: HostInputNames,
    mode: HostIdentityMode,
) -> tuple[str, str, str, str]:
    keys = (names.host_a, names.password_a, names.host_b, names.password_b)
    values = tuple(os.environ.get(key) for key in keys)
    if any(value is None or value == "" for value in values):
        missing = ", ".join(key for key, value in zip(keys, values, strict=True) if not value)
        raise RuntimeError(f"missing required external inputs: {missing}")
    host_a, password_a, host_b, password_b = values
    assert host_a is not None
    assert password_a is not None
    assert host_b is not None
    assert password_b is not None
    match mode:
        case "distinct":
            if host_a == host_b:
                raise RuntimeError("same-host mapping requires --single-host-sequential")
        case "single_sequential":
            if (host_a, password_a) != (host_b, password_b):
                raise RuntimeError("single-host mode requires exact host and password mapping")
        case unexpected:
            assert_never(unexpected)
    return host_a, password_a, host_b, password_b


def preflight_evidence(
    probe: RemoteProbe,
    *,
    mode: HostIdentityMode,
    role: PreflightRole,
) -> dict[str, JsonValue]:
    match mode:
        case "distinct":
            identities_distinct = True
        case "single_sequential":
            if role != "host_a":
                raise ValueError("single-host preflight cannot claim Host B provenance")
            identities_distinct = False
        case unexpected:
            assert_never(unexpected)
    return {
        **probe.to_dict(),
        "host_identities_distinct": identities_distinct,
        "host_identity_mode": mode,
        "provenance_role": role,
        "schema_version": 1,
        "temporal_clean_epoch_evidence": False,
    }


def parse_preflight(
    raw_output: str,
    namespace: ProofNamespace,
    *,
    ssh_key: str,
    boot_id: str,
) -> RemoteProbe:
    try:
        raw = json.loads(raw_output)
    except json.JSONDecodeError as error:
        raise RemoteProofError("remote identity probe returned invalid JSON") from error
    try:
        payload = require_mapping(raw, "remote identity probe")
        require_keys(
            payload,
            {"schema_version", "machine_id", "architecture", "planes"},
            "remote identity probe",
        )
        if payload["schema_version"] != 2:
            raise CaptureSchemaError("remote identity probe schema is unsupported")
        machine = require_text(payload["machine_id"], "remote machine identity")
        architecture = _architecture(payload["architecture"])
        inventory, plane_states, plane_provenance = _parse_planes(
            require_mapping(payload["planes"], "remote namespace planes")
        )
        normalized_boot = boot_id.strip()
        if not _BOOT_ID.fullmatch(normalized_boot):
            raise CaptureSchemaError("remote boot identity is invalid")
    except CaptureSchemaError as error:
        raise RemoteProofError(str(error)) from error
    expected = namespace.to_dict()
    clean = all(
        not _plane_has_owned_name(inventory[plane], expected[plane], namespace.stack_name)
        for plane in inventory
    )
    return RemoteProbe(
        machine_sha256=hashlib.sha256(machine.encode()).hexdigest(),
        ssh_sha256=hashlib.sha256(ssh_key.encode("ascii")).hexdigest(),
        boot_sha256=hashlib.sha256(normalized_boot.encode("ascii")).hexdigest(),
        architecture=architecture,
        namespace_clean=clean,
        inventory=inventory,
        plane_states=plane_states,
        plane_provenance=plane_provenance,
    )


def _architecture(value: JsonValue) -> str:
    raw = require_text(value, "remote architecture")
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(raw, raw)
    if architecture not in _SUPPORTED_ARCHITECTURES:
        raise CaptureSchemaError("remote host architecture is unsupported")
    return architecture


def _parse_planes(
    planes: dict[str, JsonValue],
) -> tuple[dict[str, tuple[ObservedResource, ...]], dict[str, str], dict[str, str]]:
    names = ("docker", "dokploy", "cloudflare", "tailscale", "coder")
    if set(planes) != set(names):
        raise CaptureSchemaError("remote namespace planes are incomplete")
    inventory: dict[str, tuple[ObservedResource, ...]] = {}
    states: dict[str, str] = {}
    provenance: dict[str, str] = {}
    for plane in names:
        source = require_mapping(planes[plane], f"{plane} resource objects")
        required = {"resources", "state"}
        if plane == "docker":
            required.add("provenance")
        require_keys(source, required, f"{plane} resource objects")
        state = require_text(source["state"], f"{plane} plane state")
        if plane == "docker":
            marker = require_text(source["provenance"], "docker plane provenance")
            if marker not in {"docker_absent_clean", "docker_available_inventory", "docker_error"}:
                raise CaptureSchemaError("docker plane provenance is invalid")
            if (state == "error") != (marker == "docker_error"):
                raise CaptureSchemaError("docker plane provenance is inconsistent")
            if state == "present" and marker != "docker_available_inventory":
                raise CaptureSchemaError("docker plane provenance is inconsistent")
            provenance[plane] = marker
        if state == "error":
            raise CaptureSchemaError(f"{plane} plane collection failed")
        if state not in {"absent", "present"}:
            raise CaptureSchemaError(f"{plane} plane state is invalid")
        resources = _resources(require_list(source["resources"], f"{plane} resources"), plane)
        if (state == "absent") != (not resources):
            raise CaptureSchemaError(f"{plane} plane absence is inconsistent")
        inventory[plane], states[plane] = resources, state
    return inventory, states, provenance


def _resources(values: list[JsonValue], plane: str) -> tuple[ObservedResource, ...]:
    resources = tuple(
        ObservedResource(
            resource_id=require_text(require_mapping(value, plane).get("id"), f"{plane} id"),
            kind=require_text(require_mapping(value, plane).get("kind"), f"{plane} kind"),
            name=require_text(require_mapping(value, plane).get("name"), f"{plane} name"),
        )
        for value in values
    )
    if len({resource.resource_id for resource in resources}) != len(resources):
        raise CaptureSchemaError(f"{plane} resource IDs must be unique")
    return tuple(sorted(resources, key=lambda item: (item.kind, item.resource_id)))


def _plane_has_owned_name(
    observed: tuple[ObservedResource, ...], expected: list[str] | str, stack_name: str
) -> bool:
    targets = {expected} if isinstance(expected, str) else set(expected)
    return any(
        resource.name in targets or resource.name.startswith(f"{stack_name}-")
        for resource in observed
    )
