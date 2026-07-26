from __future__ import annotations

from urllib import request

import pytest

from dokploy_wizard.networking.cloudflare import CloudflareApiBackend
from dokploy_wizard.state import RawEnvInput


class EmptyResponse:
    def __enter__(self) -> "EmptyResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b""


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


def test_delete_tunnel_cleans_connections_before_deleting_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    backend = CloudflareApiBackend(
        RawEnvInput(format_version=1, values={"CLOUDFLARE_API_TOKEN": "token-123"})
    )
    calls: list[dict[str, object]] = []

    def record_request(**call: object) -> dict[str, object]:
        calls.append(call)
        return {}

    monkeypatch.setattr(backend, "_request_json", record_request)

    # When
    backend.delete_tunnel("account-123", "tunnel-123")

    # Then
    assert calls == [
        {
            "allow_empty": True,
            "method": "DELETE",
            "path": "/accounts/account-123/cfd_tunnel/tunnel-123/connections",
        },
        {
            "method": "DELETE",
            "path": "/accounts/account-123/cfd_tunnel/tunnel-123",
        },
    ]


def test_request_json_accepts_empty_success_when_endpoint_has_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    backend = CloudflareApiBackend(
        RawEnvInput(format_version=1, values={"CLOUDFLARE_API_TOKEN": "token-123"})
    )
    monkeypatch.setattr(request, "urlopen", lambda _request: EmptyResponse())

    # When
    payload = backend._request_json(
        method="DELETE",
        path="/accounts/account-123/cfd_tunnel/tunnel-123/connections",
        allow_empty=True,
    )

    # Then
    assert payload == {}
