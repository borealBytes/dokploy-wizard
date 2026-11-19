# mypy: ignore-errors
# pyright: reportMissingImports=false

from __future__ import annotations

import subprocess
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


@pytest.fixture(autouse=True)
def _no_docker_for_placeholder_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # The placeholder probe in ensure_public_route must not touch the real
    # docker socket of whoever runs the suite; tests that exercise it re-patch.
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)


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


def _matching_route_file(tmp_path: Path) -> Path:
    route_file = tmp_path / "dokploy.yml"
    route_file.write_text(
        bootstrap._render_dokploy_public_route("dokploy.example.com"),
        encoding="utf-8",
    )
    return route_file


def test_ensure_public_route_evicts_setup_placeholder_before_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route_file = _matching_route_file(tmp_path)
    events: list[object] = []

    def fake_run(argv: list[str], **_kwargs: object):
        events.append(list(argv))
        stdout = "dokploy-postgres.1.abc123\n" if argv[:2] == ["docker", "ps"] else "DELETE 1\n"
        return subprocess.CompletedProcess(argv, returncode=0, stdout=stdout, stderr="")

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == bootstrap.LOCAL_HEALTH_URL

        def assign_domain_server(self, **kwargs: object) -> dict[str, object]:
            events.append("auth")
            return {"host": kwargs["host"], "https": kwargs["https"]}

    monkeypatch.setattr(bootstrap, "Path", lambda _path: route_file)
    monkeypatch.setattr(bootstrap, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    backend = ShellDokployBootstrapBackend(_raw_env_with_admin_creds())

    backend.ensure_public_route()

    assert events[0][:4] == ["docker", "ps", "--filter", "name=dokploy-postgres"]
    exec_call = events[1]
    assert isinstance(exec_call, list)
    assert exec_call[:3] == ["docker", "exec", "dokploy-postgres.1.abc123"]
    assert exec_call[-1] == bootstrap._DOKPLOY_PLACEHOLDER_CLEANUP_SQL
    # Passwordless-placeholder scope, and the eviction must precede auth: the
    # sign-up endpoint refuses creators while the placeholder owns the org.
    assert "nxploited@dokploy.com" in bootstrap._DOKPLOY_PLACEHOLDER_CLEANUP_SQL
    assert "not exists (select 1 from account" in bootstrap._DOKPLOY_PLACEHOLDER_CLEANUP_SQL
    assert events[2] == "auth"


def test_ensure_public_route_without_docker_still_authenticates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route_file = _matching_route_file(tmp_path)
    calls: list[dict[str, object]] = []

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == bootstrap.LOCAL_HEALTH_URL

        def assign_domain_server(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"host": kwargs["host"], "https": kwargs["https"]}

    monkeypatch.setattr(bootstrap, "Path", lambda _path: route_file)
    monkeypatch.setattr(bootstrap, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no subprocess without docker")),
    )
    # The autouse fixture keeps shutil.which at None: no docker, no probe.

    backend = ShellDokployBootstrapBackend(_raw_env_with_admin_creds())

    backend.ensure_public_route()

    assert len(calls) == 1
