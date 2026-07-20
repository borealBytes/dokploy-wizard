"""Bounded, read-only Paramiko probes used before the proof wrapper uploads files."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dokploy_wizard.remote import capture_remote_output
from dokploy_wizard.remote_transport import ParamikoRemoteTransport


@dataclass(frozen=True, slots=True)
class RemoteProofError(RuntimeError):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class RemoteProbe:
    machine_sha256: str
    ssh_sha256: str
    architecture: str
    namespace_clean: bool


def probe_host(
    *, host: str, password: str, stack_name: str, timeout_seconds: int = 30
) -> RemoteProbe:
    """Read a host identity and owned namespace without uploading or mutating anything."""
    transport = ParamikoRemoteTransport.connect(
        hostname=host,
        username="root",
        password=password,
        remote_root="/root/dokploy-wizard",
        timeout=timeout_seconds,
    )
    try:
        remote_key = transport.client.get_transport().get_remote_server_key()
        key_fingerprint = remote_key.get_fingerprint().hex()
        output = capture_remote_output(
            transport,
            "machine=$(cat /etc/machine-id); arch=$(uname -m); "
            "names=$(command -v docker >/dev/null 2>&1 && "
            "docker ps -a --format '{{.Names}}' || true); "
            "printf '%s\\n%s\\n%s\\n' \"$machine\" \"$arch\" \"$names\"",
            timeout_seconds=timeout_seconds,
        )
    finally:
        transport.close()
    lines = output.splitlines()
    if len(lines) < 2:
        raise RemoteProofError("remote identity probe returned an invalid response")
    machine, raw_architecture, *names = lines
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(raw_architecture, raw_architecture)
    if architecture not in {"amd64", "arm64"}:
        raise RemoteProofError("remote host architecture is unsupported")
    namespace_clean = not any(name.startswith(stack_name) for name in names if name)
    return RemoteProbe(
        machine_sha256=hashlib.sha256(machine.encode("utf-8")).hexdigest(),
        ssh_sha256=hashlib.sha256(key_fingerprint.encode("ascii")).hexdigest(),
        architecture=architecture,
        namespace_clean=namespace_clean,
    )
