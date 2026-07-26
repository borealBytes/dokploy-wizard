# ruff: noqa: E501
"""Bounded read-only SSH probes and post-wrapper snapshot transport."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from dokploy_wizard.proof.model_sync_env import ProofNamespace, resolve_proof_transport
from dokploy_wizard.proof.model_sync_identity import (
    ObservedResource as ObservedResource,
)
from dokploy_wizard.proof.model_sync_identity import (
    RemoteProbe as RemoteProbe,
)
from dokploy_wizard.proof.model_sync_identity import (
    RemoteProofError as RemoteProofError,
)
from dokploy_wizard.proof.model_sync_identity import (
    parse_preflight as parse_preflight,
)
from dokploy_wizard.proof.model_sync_results import (
    PREFLIGHT_SCRIPT,
    ProofTransport,
    run_bounded_process,
)
from dokploy_wizard.remote import capture_remote_output
from dokploy_wizard.remote_transport import ParamikoRemoteTransport

__all__ = (
    "ObservedResource",
    "ParamikoRemoteTransport",
    "RemoteProbe",
    "RemoteProofError",
    "capture_host_a_snapshot",
    "parse_preflight",
    "probe_host",
)


def probe_host(
    *,
    host: str,
    password: str,
    namespace: ProofNamespace,
    proof_transport: ProofTransport,
    timeout_seconds: int = 30,
) -> RemoteProbe:
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
        boot_id = capture_remote_output(
            transport,
            "# model-sync-boot-id\ncat /proc/sys/kernel/random/boot_id",
            timeout_seconds=timeout_seconds,
        )
        try:
            output = capture_remote_output(
                transport,
                _preflight_command(),
                timeout_seconds=timeout_seconds,
                stdin_bytes=_transport_bytes(proof_transport),
            )
        except RuntimeError as error:
            raise RemoteProofError("remote preflight transport failed") from error
    finally:
        transport.close()
    return parse_preflight(output, namespace, ssh_key=key, boot_id=boot_id)


def capture_host_a_snapshot(
    *,
    host: str,
    password: str,
    timeout_seconds: int = 120,
    task1_context: bool = False,
) -> str:
    """Collect the remote value-free Coder/resource snapshot after wrapper success."""
    capture_timeout_seconds = max(600, timeout_seconds) if task1_context else timeout_seconds
    transport = ParamikoRemoteTransport.connect(
        hostname=host,
        username="root",
        password=password,
        remote_root="/root/dokploy-wizard",
        timeout=capture_timeout_seconds,
    )
    try:
        command = "cd /root/dokploy-wizard && PYTHONPATH=./src python3 -m dokploy_wizard.proof.model_sync_host_b model-sync-snapshot --env-file .install.env --state-dir state"
        if task1_context:
            command += " --task1-proof-context task1-proof-context.json"
        return capture_remote_output(
            transport,
            command,
            timeout_seconds=capture_timeout_seconds,
        )
    finally:
        transport.close()


def capture_local_authoritative_inventory(env_file: Path, namespace: ProofNamespace) -> RemoteProbe:
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
    return parse_preflight(
        output,
        namespace,
        ssh_key="post-install-local",
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii"),
    )


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
            "dokploy_admin_email": transport.dokploy_admin_email,
            "dokploy_admin_password": transport.dokploy_admin_password,
            "tailscale_required": transport.tailscale_required,
        },
        separators=(",", ":"),
    ).encode()
