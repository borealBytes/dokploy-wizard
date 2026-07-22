"""Exact proof namespace construction."""

from __future__ import annotations

from dokploy_wizard.proof import ProofNamespace
from dokploy_wizard.state.models import DesiredState


def build_proof_namespace(values: dict[str, str], desired: DesiredState) -> ProofNamespace:
    """Build all exact non-secret resource names from validated proof state."""
    stack = desired.stack_name
    shared = desired.shared_core
    docker = [stack, f"{stack}-coder", f"{stack}-cloudflared", shared.network_name]
    docker.extend(
        service.service_name
        for service in (shared.litellm, shared.postgres, shared.redis, shared.mail_relay)
        if service is not None
    )
    return ProofNamespace(
        stack_name=stack,
        docker=tuple(sorted(set(docker))),
        dokploy=tuple(sorted({stack, f"{stack}-coder", f"{stack}-shared"})),
        cloudflare=tuple(
            sorted(
                {
                    stack,
                    f"{stack}-cloudflared",
                    values.get("CLOUDFLARE_TUNNEL_NAME", f"{stack}-tunnel"),
                    *desired.hostnames.values(),
                }
            )
        ),
        tailscale=() if desired.tailscale_hostname is None else (desired.tailscale_hostname,),
        coder_templates=(
            "ubuntu-vscode",
            "ubuntu-vscode-opencode-web",
            "ubuntu-vscode-openwork",
            "ubuntu-vscode-kdense-byok",
            "ubuntu-vscode-hermes",
            "ubuntu-vscode-pi-web",
        ),
    )
