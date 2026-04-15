from dokploy_wizard.dokploy.cloudflared import _render_compose_file


def test_cloudflared_compose_uses_host_networking() -> None:
    compose = _render_compose_file(
        "wizard-stack-cloudflared",
        tunnel_token="token-123",
    )

    assert "image: cloudflare/cloudflared:latest" in compose
    assert "network_mode: host" in compose
    assert "command: ['tunnel', '--no-autoupdate', 'run']" in compose
    assert 'TUNNEL_TOKEN: "token-123"' in compose
