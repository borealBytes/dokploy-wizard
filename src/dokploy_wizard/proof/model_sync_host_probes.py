"""Pre-install host probing for Task 1 baseline proof modes."""

from __future__ import annotations

from typing import assert_never

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_env import ProofNamespace
from dokploy_wizard.proof.model_sync_host_b import HostIdentity, assert_namespace_identity
from dokploy_wizard.proof.model_sync_remote import RemoteProbe, probe_host
from dokploy_wizard.proof.model_sync_results import ProofTransport


def probe_baseline_hosts(
    *,
    host_a: str,
    password_a: str,
    host_b: str,
    password_b: str,
    mode: proof.HostIdentityMode,
    namespace: ProofNamespace,
    transport: ProofTransport,
) -> tuple[RemoteProbe, RemoteProbe | None]:
    """Probe the mode-authorized hosts and reject incompatible clean states."""
    host_a_probe = probe_host(
        host=host_a, password=password_a, namespace=namespace, proof_transport=transport
    )
    identity_a = HostIdentity(
        host_a_probe.machine_sha256, host_a_probe.ssh_sha256, host_a_probe.architecture
    )
    host_b_probe: RemoteProbe | None
    match mode:
        case "distinct":
            host_b_probe = probe_host(
                host=host_b, password=password_b, namespace=namespace, proof_transport=transport
            )
            identity_b = HostIdentity(
                host_b_probe.machine_sha256, host_b_probe.ssh_sha256, host_b_probe.architecture
            )
            assert_namespace_identity(host_a=identity_a, host_b=identity_b)
        case "single_sequential":
            host_b_probe = None
        case unexpected:
            assert_never(unexpected)
    if not host_a_probe.namespace_clean or (
        host_b_probe is not None and not host_b_probe.namespace_clean
    ):
        raise RuntimeError("managed namespace residue blocks live baseline proof")
    return host_a_probe, host_b_probe
