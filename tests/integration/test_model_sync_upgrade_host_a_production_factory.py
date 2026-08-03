from __future__ import annotations

import base64
import json
from argparse import Namespace
from pathlib import Path

import pytest

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_upgrade_host_a_production_factory as factory
from dokploy_wizard.proof import model_sync_upgrade_host_a_remote as remote
from dokploy_wizard.proof.model_sync_results import ProofTransport
from tests.integration._model_sync_upgrade_host_a_live_path_support import production_config


def test_remote_coder_login_carries_credentials_only_in_ssh_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    class FakeParamikoRemoteTransport:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    ssh = FakeParamikoRemoteTransport()

    def capture(
        _transport: FakeParamikoRemoteTransport,
        command: str,
        *,
        timeout_seconds: int,
        stdin_bytes: bytes | None = None,
    ) -> str:
        assert timeout_seconds == 60
        assert "admin-password" not in command
        assert "fixture-stack-shared" in command
        assert "fixture-stack-coder" in command
        assert stdin_bytes is not None
        payload = json.loads(stdin_bytes)
        assert payload["path"] == "/api/v2/users/login"
        assert b"admin-password" in base64.b64decode(payload["body"])
        body = base64.b64encode(b'{"session_token":"session-token"}').decode()
        return json.dumps({"status": 200, "body": body})

    monkeypatch.setattr(
        "dokploy_wizard.proof.model_sync_upgrade_host_a_remote.ParamikoRemoteTransport.connect",
        lambda **_kwargs: ssh,
    )
    monkeypatch.setattr(remote, "capture_remote_output", capture)
    transport = remote.RemoteCoderTransport(
        "fixture-host",
        "fixture-password",
        "fixture-stack",
    )

    # When
    token = transport.login("admin@example.test", "admin-password")

    # Then
    assert token == "session-token"
    assert ssh.closed is True


def test_factory_uses_task1_bound_internal_coder_transport_when_public_route_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    config = production_config(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    args = Namespace(
        wrapper=config.wrapper,
        env_file=config.env_file,
        artifact_dir=artifact_dir,
        output=artifact_dir / "result.json",
        lifecycle_output=artifact_dir / "single-host-lifecycle-host-a.json",
        host_env="FIRST_FRESH_HOST",
        password_env="FIRST_FRESH_PASSWORD",
    )
    transport = ProofTransport(
        cloudflare_account_id=None,
        cloudflare_zone_id=None,
        cloudflare_zone_name="example.test",
        cloudflare_token=None,
        dokploy_api_url=None,
        dokploy_api_key=None,
        coder_email="admin@example.test",
        coder_hostname="coder.example.test",
        coder_password="admin-password",
        tailscale_required=False,
    )
    events: list[tuple[str, ...]] = []

    class FakeRemoteCoderTransport:
        def __init__(self, host: str, password: str, stack_name: str) -> None:
            events.append(("remote", host, password, stack_name))

        def login(self, email: str, password: str) -> str:
            events.append(("login", email, password))
            return "session-token"

    class FakeCoderMigrationApi:
        def __init__(
            self,
            *,
            transport: FakeRemoteCoderTransport,
            session_token: str,
        ) -> None:
            events.append(("api", session_token))

        def default_organization_id(self) -> str:
            return "organization-id"

    def reject_public_login(*_args: str) -> str:
        raise AssertionError("public Coder login must not be used")

    monkeypatch.setenv("FIRST_FRESH_HOST", "fixture-host")
    monkeypatch.setenv("FIRST_FRESH_PASSWORD", "fixture-password")
    monkeypatch.setattr(factory, "_canonical_wrapper", lambda path: path)
    monkeypatch.setattr(factory, "_validated_env", lambda path, _binding: path)
    monkeypatch.setattr(factory, "resolve_proof_transport", lambda _path: transport)
    def reject_restored_env_namespace(_path: Path) -> proof.ProofNamespace:
        raise AssertionError("Task 18 must use the Task 1 bound proof namespace")

    monkeypatch.setattr(
        factory,
        "resolve_proof_namespace",
        reject_restored_env_namespace,
        raising=False,
    )
    monkeypatch.setattr(factory, "coder_login", reject_public_login, raising=False)
    monkeypatch.setattr(factory, "RemoteCoderTransport", FakeRemoteCoderTransport, raising=False)
    monkeypatch.setattr(factory, "CoderMigrationApi", FakeCoderMigrationApi)

    # When
    operations = factory.build_production_operations(args, config.binding)

    # Then
    assert operations is not None
    assert events == [
        ("remote", "fixture-host", "fixture-password", "stack"),
        ("login", "admin@example.test", "admin-password"),
        ("api", "session-token"),
    ]
