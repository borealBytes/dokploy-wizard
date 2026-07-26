from __future__ import annotations

import pytest

from dokploy_wizard.networking.cloudflare import CloudflareApiBackend
from dokploy_wizard.state import RawEnvInput


def test_get_tunnel_treats_provider_deletion_tombstone_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    backend = CloudflareApiBackend(
        RawEnvInput(format_version=1, values={"CLOUDFLARE_API_TOKEN": "token-123"})
    )
    monkeypatch.setattr(
        backend,
        "_request_json",
        lambda **_: {
            "result": {
                "config_src": "cloudflare",
                "deleted_at": "2026-07-25T23:59:00Z",
                "id": "tunnel-123",
                "name": "task1-tunnel",
                "status": "down",
            }
        },
    )

    # When
    tunnel = backend.get_tunnel("account-123", "tunnel-123")

    # Then
    assert tunnel is None
