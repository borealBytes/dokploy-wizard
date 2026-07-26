# mypy: ignore-errors
# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard import bootstrap
from dokploy_wizard.bootstrap import DokployBootstrapError, ShellDokployBootstrapBackend
from dokploy_wizard.state import RawEnvInput


def _raw_env_with_admin_creds() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        },
    )


def test_ensure_public_route_assigns_domain_server_when_route_file_already_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route_file = tmp_path / "dokploy.yml"
    route_file.write_text(
        bootstrap._render_dokploy_public_route("dokploy.example.com"),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == bootstrap.LOCAL_HEALTH_URL

        def assign_domain_server(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"host": kwargs["host"], "https": kwargs["https"]}

    monkeypatch.setattr(bootstrap, "Path", lambda _path: route_file)
    monkeypatch.setattr(bootstrap, "DokployBootstrapAuthClient", FakeAuthClient)

    backend = ShellDokployBootstrapBackend(_raw_env_with_admin_creds())

    backend.ensure_public_route()

    assert route_file.read_text(encoding="utf-8") == bootstrap._render_dokploy_public_route(
        "dokploy.example.com"
    )
    assert calls == [
        {
            "admin_email": "admin@example.com",
            "admin_password": "secret-123",
            "host": "dokploy.example.com",
            "certificate_type": "none",
            "lets_encrypt_email": "",
            "https": True,
        }
    ]


def test_ensure_public_route_raises_when_assign_domain_server_auth_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route_file = tmp_path / "dokploy.yml"
    route_file.write_text(
        bootstrap._render_dokploy_public_route("dokploy.example.com"),
        encoding="utf-8",
    )

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == bootstrap.LOCAL_HEALTH_URL

        def assign_domain_server(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            raise bootstrap.DokployBootstrapAuthError("Invalid origin bootstrap auth failed")

    monkeypatch.setattr(bootstrap, "Path", lambda _path: route_file)
    monkeypatch.setattr(bootstrap, "DokployBootstrapAuthClient", FakeAuthClient)

    backend = ShellDokployBootstrapBackend(_raw_env_with_admin_creds())

    with pytest.raises(DokployBootstrapError, match="Invalid origin bootstrap auth failed"):
        backend.ensure_public_route()
