# ruff: noqa: E501
"""Bounded read-only SSH probes and post-wrapper snapshot transport."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from typing import Final

from dokploy_wizard.proof.model_sync_artifacts import (
    CaptureSchemaError,
    JsonValue,
    require_keys,
    require_list,
    require_mapping,
    require_text,
)
from dokploy_wizard.proof.model_sync_env import ProofNamespace
from dokploy_wizard.remote import capture_remote_output
from dokploy_wizard.remote_transport import ParamikoRemoteTransport

_SUPPORTED_ARCHITECTURES: Final = frozenset({"amd64", "arm64"})
__all__ = ("ParamikoRemoteTransport", "RemoteProbe", "capture_host_a_snapshot", "probe_host")
_PREFLIGHT_SCRIPT: Final = (
    "import json,platform,subprocess; "
    "run=lambda *a: subprocess.run(a,capture_output=True,text=True,check=False).stdout.splitlines(); "
    "docker=run('docker','ps','-a','--format','{{.Names}}')+run('docker','network','ls','--format','{{.Name}}')+run('docker','volume','ls','--format','{{.Name}}'); "
    "print(json.dumps({'schema_version':1,'machine_id':open('/etc/machine-id').read().strip(),'architecture':platform.machine(),'planes':{'docker':docker,'dokploy':docker,'cloudflare':docker,'tailscale':run('tailscale','status','--json'),'coder':docker}}))"
)


@dataclass(frozen=True, slots=True)
class RemoteProofError(RuntimeError):
    """Raised when an SSH proof response cannot prove the required invariant."""

    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class RemoteProbe:
    """Hashed host identity plus complete, non-secret namespace inventory."""

    machine_sha256: str
    ssh_sha256: str
    architecture: str
    namespace_clean: bool
    inventory: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, JsonValue]:
        """Render non-secret preflight evidence only after all captures are complete."""
        return {
            "architecture": self.architecture,
            "inventory": {key: list(value) for key, value in sorted(self.inventory.items())},
            "machine_sha256": self.machine_sha256,
            "namespace_clean": self.namespace_clean,
            "ssh_sha256": self.ssh_sha256,
        }


def probe_host(*, host: str, password: str, namespace: ProofNamespace, timeout_seconds: int = 30) -> RemoteProbe:
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
        output = capture_remote_output(transport, _preflight_command(namespace), timeout_seconds=timeout_seconds)
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


def _parse_preflight(raw_output: str, namespace: ProofNamespace, ssh_key: str) -> RemoteProbe:
    try:
        raw = json.loads(raw_output)
    except json.JSONDecodeError as error:
        raise RemoteProofError("remote identity probe returned invalid JSON") from error
    try:
        payload = require_mapping(raw, "remote identity probe")
        require_keys(payload, {"schema_version", "machine_id", "architecture", "planes"}, "remote identity probe")
        if payload["schema_version"] != 1:
            raise CaptureSchemaError("remote identity probe schema is unsupported")
        machine = require_text(payload["machine_id"], "remote machine identity")
        raw_architecture = require_text(payload["architecture"], "remote architecture")
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(raw_architecture, raw_architecture)
        if architecture not in _SUPPORTED_ARCHITECTURES:
            raise CaptureSchemaError("remote host architecture is unsupported")
        planes = require_mapping(payload["planes"], "remote namespace planes")
        expected = namespace.to_dict()
        inventory: dict[str, tuple[str, ...]] = {}
        for plane in ("docker", "dokploy", "cloudflare", "tailscale", "coder"):
            inventory[plane] = tuple(sorted(_unique(require_list(planes.get(plane), f"{plane} inventory"), plane)))
        if set(planes) != {"docker", "dokploy", "cloudflare", "tailscale", "coder"}:
            raise CaptureSchemaError("remote namespace planes are incomplete")
    except CaptureSchemaError as error:
        raise RemoteProofError(str(error)) from error
    clean = all(not _plane_has_owned_name(inventory[plane], expected[plane], namespace.stack_name) for plane in inventory)
    return RemoteProbe(
        machine_sha256=hashlib.sha256(machine.encode()).hexdigest(),
        ssh_sha256=hashlib.sha256(ssh_key.encode("ascii")).hexdigest(),
        architecture=architecture,
        namespace_clean=clean,
        inventory=inventory,
    )


def _preflight_command(namespace: ProofNamespace) -> str:
    del namespace
    return f"# model-sync-preflight\npython3 -c {shlex.quote(_PREFLIGHT_SCRIPT)}"


def _unique(values: list[JsonValue], plane: str) -> list[str]:
    names = [require_text(value, f"{plane} namespace identifier") for value in values]
    if len(names) != len(set(names)):
        raise CaptureSchemaError(f"{plane} namespace inventory contains duplicate identifiers")
    return names


def _plane_has_owned_name(observed: tuple[str, ...], expected: list[str] | str, stack_name: str) -> bool:
    names = [expected] if isinstance(expected, str) else expected
    targets = set(names)
    return any(name in targets or name.startswith(f"{stack_name}-") for name in observed)
