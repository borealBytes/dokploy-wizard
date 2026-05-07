from pathlib import Path

import pytest

from dokploy_wizard.dokploy import DokployCloudflaredBackend
from dokploy_wizard.dokploy.cloudflared import _render_compose_file
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    write_applied_checkpoint,
)

from .fake_dokploy import FakeDokployApiClient


def test_cloudflared_compose_uses_host_networking() -> None:
    compose = _render_compose_file(
        "wizard-stack-cloudflared",
        tunnel_token="token-123",
    )

    assert "image: cloudflare/cloudflared:latest" in compose
    assert "network_mode: host" in compose
    assert "command: ['tunnel', '--no-autoupdate', 'run']" in compose
    assert 'TUNNEL_TOKEN: "token-123"' in compose


def test_dokploy_cloudflared_backend_skips_redeploy_when_hash_matches_and_container_is_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service_name = "wizard-stack-cloudflared"
    compose_file = _render_compose_file(service_name, tunnel_token="token-123")
    _write_hash_checkpoint(tmp_path, service_name=service_name, rendered_compose=compose_file)
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-cloudflared",
        project_name="wizard-stack",
        compose_file=compose_file,
    )
    backend = DokployCloudflaredBackend(
        api_url="https://dokploy.example.com",
        api_key="dokp-key-123",
        state_dir=tmp_path,
        stack_name="wizard-stack",
        public_url="https://dokploy.example.com",
        client=client,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.cloudflared._docker_container_is_up",
        lambda current_service_name: current_service_name == service_name,
    )

    record = backend.create_service(resource_name=service_name, tunnel_token="token-123")

    assert record.resource_id == "dokploy-compose:cmp-cloudflared:cloudflared"
    client.assert_unchanged_service(service_name)


def _write_hash_checkpoint(state_dir: Path, *, service_name: str, rendered_compose: str) -> None:
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="fingerprint",
            completed_steps=("networking",),
            compose_artifact_hashes={
                service_name: ComposeArtifactHashState.from_rendered_compose(
                    service_id=service_name,
                    rendered_compose=rendered_compose,
                )
            },
        ),
    )
