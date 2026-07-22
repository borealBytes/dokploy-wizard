"""Typed host identity and preflight evidence for model-sync proofs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Final

from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    require_keys,
    require_list,
    require_mapping,
    require_text,
)
from dokploy_wizard.proof.model_sync_cloudflare import (
    CloudflareMatch,
    CloudflareProvenance,
    CloudflareResource,
    preexisting_cloudflare_sha256,
)
from dokploy_wizard.proof.model_sync_cloudflare_probe import (
    build_cloudflare_resources,
    verify_cloudflare_resources,
)
from dokploy_wizard.proof.model_sync_env import ProofNamespace

_SUPPORTED_ARCHITECTURES: Final = frozenset({"amd64", "arm64"})
_BOOT_ID: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class RemoteProofError(RuntimeError):
    """Raised when remote proof evidence violates its typed contract."""


@dataclass(frozen=True, slots=True)
class ObservedResource:
    resource_id: str
    name: str
    kind: str
    fingerprint_sha256: str | None = None
    match: CloudflareMatch | None = None
    provenance: CloudflareProvenance | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        if self.fingerprint_sha256 is not None:
            if self.match is None or self.provenance is None:
                raise RemoteProofError("Cloudflare resource evidence is incomplete")
            return {
                "fingerprint_sha256": self.fingerprint_sha256,
                "id": self.resource_id,
                "kind": self.kind,
                "match": self.match,
                "provenance": self.provenance,
            }
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

    @property
    def preexisting_cloudflare_sha256(self) -> str:
        resources = tuple(
            CloudflareResource(
                item.resource_id,
                item.kind,
                item.fingerprint_sha256,
                item.match,
                item.provenance,
            )
            for item in self.inventory["cloudflare"]
            if item.fingerprint_sha256 is not None
            and item.match is not None
            and item.provenance is not None
        )
        return preexisting_cloudflare_sha256(resources)

    def verify_preexisting_cloudflare_unchanged(self, post_install: RemoteProbe) -> None:
        verify_cloudflare_resources(
            self.inventory["cloudflare"],
            post_install.inventory["cloudflare"],
        )


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
            require_mapping(payload["planes"], "remote namespace planes"), namespace
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
        if plane != "cloudflare"
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
    namespace: ProofNamespace,
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
        raw_resources = require_list(source["resources"], f"{plane} resources")
        resources = (
            build_cloudflare_resources(raw_resources, namespace, ObservedResource)
            if plane == "cloudflare"
            else _resources(raw_resources, plane)
        )
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
