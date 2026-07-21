# ruff: noqa: E501
"""Bounded read-only SSH probes and post-wrapper snapshot transport."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    require_keys,
    require_list,
    require_mapping,
    require_text,
)
from dokploy_wizard.proof.model_sync_env import ProofNamespace, resolve_proof_transport
from dokploy_wizard.proof.model_sync_results import (
    PREFLIGHT_SCRIPT,
    ProofTransport,
    run_bounded_process,
)
from dokploy_wizard.remote import capture_remote_output
from dokploy_wizard.remote_transport import ParamikoRemoteTransport

_SUPPORTED_ARCHITECTURES: Final = frozenset({"amd64", "arm64"})
__all__ = ("ParamikoRemoteTransport", "RemoteProbe", "capture_host_a_snapshot", "probe_host")


@dataclass(frozen=True, slots=True)
class RemoteProofError(RuntimeError):
    """Raised when an SSH proof response cannot prove the required invariant."""

    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class ObservedResource:
    """One stable resource ID observed through its own read-only plane."""

    resource_id: str
    name: str
    kind: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.resource_id, "kind": self.kind, "name": self.name}


@dataclass(frozen=True, slots=True)
class RemoteProbe:
    """Hashed host identity plus complete, non-secret namespace inventory."""

    machine_sha256: str
    ssh_sha256: str
    architecture: str
    namespace_clean: bool
    inventory: dict[str, tuple[ObservedResource, ...]]
    plane_states: dict[str, str]

    def to_dict(self) -> dict[str, JsonValue]:
        """Render non-secret preflight evidence only after all captures are complete."""
        return {
            "architecture": self.architecture,
            "inventory": {
                key: {
                    "resources": [resource.to_dict() for resource in value],
                    "state": self.plane_states[key],
                }
                for key, value in sorted(self.inventory.items())
            },
            "machine_sha256": self.machine_sha256,
            "namespace_clean": self.namespace_clean,
            "ssh_sha256": self.ssh_sha256,
        }


def probe_host(*, host: str, password: str, namespace: ProofNamespace, proof_transport: ProofTransport, timeout_seconds: int = 30) -> RemoteProbe:
    """Read every exact resource plane before upload and reject stale owned namespaces."""
    transport = ParamikoRemoteTransport.connect(
        hostname=host,
        username="root",
        password=password,
        remote_root="/root/dokploy-wizard",
        timeout=timeout_seconds,
    )
    try:
        key = transport.client.get_transport().get_remote_server_key().get_fingerprint().hex()
        output = capture_remote_output(
            transport,
            _preflight_command(),
            timeout_seconds=timeout_seconds,
            stdin_bytes=_transport_bytes(proof_transport),
        )
    finally:
        transport.close()
    return _parse_preflight(output, namespace, key)


def capture_host_a_snapshot(*, host: str, password: str, timeout_seconds: int = 120) -> str:
    """Collect the remote value-free Coder/resource snapshot after wrapper success."""
    transport = ParamikoRemoteTransport.connect(
        hostname=host,
        username="root",
        password=password,
        remote_root="/root/dokploy-wizard",
        timeout=timeout_seconds,
    )
    try:
        return capture_remote_output(
            transport,
            "cd /root/dokploy-wizard && PYTHONPATH=./src python3 -m dokploy_wizard.proof.model_sync_host_b model-sync-snapshot --env-file .install.env --state-dir state",
            timeout_seconds=timeout_seconds,
        )
    finally:
        transport.close()
def capture_local_authoritative_inventory(
    env_file: Path, namespace: ProofNamespace
) -> RemoteProbe:
    """Reuse the pre-upload collectors after installation without exposing credentials."""
    try:
        encoded = run_bounded_process(
            [sys.executable, "-c", PREFLIGHT_SCRIPT],
            stdin=_transport_bytes(resolve_proof_transport(env_file)),
            output_limit=2 * 1024 * 1024,
            timeout_seconds=120,
            label="post-install authoritative inventory",
        )
    except RuntimeError as error:
        raise RemoteProofError("post-install authoritative inventory failed") from error
    try:
        output = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RemoteProofError("post-install inventory returned invalid UTF-8") from error
    return _parse_preflight(output, namespace, "post-install-local")
def _parse_preflight(raw_output: str, namespace: ProofNamespace, ssh_key: str) -> RemoteProbe:
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
        raw_architecture = require_text(payload["architecture"], "remote architecture")
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(
            raw_architecture, raw_architecture
        )
        if architecture not in _SUPPORTED_ARCHITECTURES:
            raise CaptureSchemaError("remote host architecture is unsupported")
        planes = require_mapping(payload["planes"], "remote namespace planes")
        inventory, plane_states = _parse_planes(planes)
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
        architecture=architecture,
        namespace_clean=clean,
        inventory=inventory,
        plane_states=plane_states,
    )


def _parse_planes(
    planes: dict[str, JsonValue],
) -> tuple[dict[str, tuple[ObservedResource, ...]], dict[str, str]]:
    names = ("docker", "dokploy", "cloudflare", "tailscale", "coder")
    if set(planes) != set(names):
        raise CaptureSchemaError("remote namespace planes are incomplete")
    inventory: dict[str, tuple[ObservedResource, ...]] = {}
    states: dict[str, str] = {}
    for plane in names:
        source = require_mapping(planes[plane], f"{plane} resource objects")
        require_keys(source, {"resources", "state"}, f"{plane} resource objects")
        state = require_text(source["state"], f"{plane} plane state")
        if state == "error":
            raise CaptureSchemaError(f"{plane} plane collection failed")
        if state not in {"absent", "present"}:
            raise CaptureSchemaError(f"{plane} plane state is invalid")
        resources = _resources(require_list(source["resources"], f"{plane} resources"), plane)
        if (state == "absent") != (not resources):
            raise CaptureSchemaError(f"{plane} plane absence is inconsistent")
        inventory[plane], states[plane] = resources, state
    return inventory, states


def _preflight_command() -> str:
    return f"# model-sync-preflight\npython3 -c {shlex.quote(PREFLIGHT_SCRIPT)}"


def _transport_bytes(transport: ProofTransport) -> bytes:
    return json.dumps(
        {
            "cloudflare_account_id": transport.cloudflare_account_id,
            "cloudflare_token": transport.cloudflare_token,
            "cloudflare_zone_id": transport.cloudflare_zone_id,
            "cloudflare_zone_name": transport.cloudflare_zone_name,
            "coder_email": transport.coder_email,
            "coder_hostname": transport.coder_hostname,
            "coder_password": transport.coder_password,
            "dokploy_api_key": transport.dokploy_api_key,
            "dokploy_api_url": transport.dokploy_api_url,
            "tailscale_required": transport.tailscale_required,
        },
        separators=(",", ":"),
    ).encode()


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
