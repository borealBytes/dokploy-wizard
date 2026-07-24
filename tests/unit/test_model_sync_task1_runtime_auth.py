from __future__ import annotations

import stat
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.dokploy.bootstrap_auth import DokployBootstrapAuthResult
from dokploy_wizard.proof.model_sync_task1_context import (
    activate_task1_proof_context,
    derive_task1_proof_context,
)
from dokploy_wizard.proof.model_sync_task1_materialization import (
    materialize_task1_external_files,
)
from dokploy_wizard.state import parse_env_file, resolve_desired_state
from dokploy_wizard.state.dokploy_runtime_auth import (
    load_dokploy_runtime_auth,
    merge_dokploy_runtime_auth,
)


def test_context_active_auth_persists_only_in_state_and_keeps_upload_exact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    source = (
        b"ROOT_DOMAIN=example.test\n"
        b"PACKS=coder\n"
        b"DOKPLOY_ADMIN_EMAIL=admin@example.test\n"
        b"DOKPLOY_ADMIN_PASSWORD=admin-password\n"
    )
    source_path = tmp_path / "install.env"
    source_path.write_bytes(source)
    source_path.chmod(0o600)
    preparation = derive_task1_proof_context(
        source_values=parse_env_file(source_path).values,
        source_bytes=source,
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(preparation.materialization)
    upload_bytes = preparation.uploaded_env_file.read_bytes()
    upload_mode = stat.S_IMODE(preparation.uploaded_env_file.stat().st_mode)

    class BootstrapBackend:
        def is_healthy(self) -> bool:
            return True

        def install(self) -> None:
            raise AssertionError("healthy Dokploy must not install")

    class AuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str
        ) -> DokployBootstrapAuthResult:
            assert admin_email == "admin@example.test"
            assert admin_password == "admin-password"
            assert key_name.startswith("dokploy-wizard-")
            return DokployBootstrapAuthResult(
                api_key="generated-key",
                api_url="http://127.0.0.1:3000",
                admin_email=admin_email,
                organization_id="org-1",
                used_sign_up=False,
                auth_path="/api/auth/sign-in/email",
                session_path="/api/user.session",
            )

    class DokployClient:
        def __init__(self, *, api_url: str, api_key: str) -> None:
            assert api_url == "http://127.0.0.1:3000"
            assert api_key == "generated-key"

        def list_projects(self) -> tuple[object, ...]:
            return ()

    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", AuthClient)
    monkeypatch.setattr(cli, "DokployApiClient", DokployClient)
    raw_env = parse_env_file(preparation.uploaded_env_file)
    with activate_task1_proof_context(preparation.context):
        desired_state = resolve_desired_state(raw_env)

    # When
    with activate_task1_proof_context(preparation.context):
        updated = cli._ensure_dokploy_api_auth(
            env_file=preparation.uploaded_env_file,
            state_dir=tmp_path / "state",
            raw_env=raw_env,
            desired_state=desired_state,
            bootstrap_backend=BootstrapBackend(),
            dry_run=False,
            require_real_dokploy_auth=True,
        )

    # Then
    assert updated.values["DOKPLOY_API_KEY"] == "generated-key"
    assert preparation.uploaded_env_file.read_bytes() == upload_bytes
    assert stat.S_IMODE(preparation.uploaded_env_file.stat().st_mode) == upload_mode
    runtime_auth = load_dokploy_runtime_auth(tmp_path / "state")
    assert runtime_auth is not None
    assert runtime_auth.api_key == "generated-key"
    merged = merge_dokploy_runtime_auth(raw_env, runtime_auth)
    assert merged.values["DOKPLOY_API_URL"] == "http://127.0.0.1:3000"
    assert merged.values["DOKPLOY_API_KEY"] == "generated-key"


def test_non_context_auth_still_persists_to_operator_env(tmp_path: Path) -> None:
    # Given
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        "ROOT_DOMAIN=example.test\nDOKPLOY_BOOTSTRAP_MOCK_API_KEY=reusable-key\n",
        encoding="utf-8",
    )
    env_file.chmod(0o640)
    raw_env = parse_env_file(env_file)
    desired_state = resolve_desired_state(raw_env)

    class UnusedBootstrapBackend:
        def is_healthy(self) -> bool:
            raise AssertionError("mock API auth must not probe Dokploy")

        def install(self) -> None:
            raise AssertionError("mock API auth must not install Dokploy")

    # When
    updated = cli._ensure_dokploy_api_auth(
        env_file=env_file,
        raw_env=raw_env,
        desired_state=desired_state,
        bootstrap_backend=UnusedBootstrapBackend(),
        dry_run=False,
        require_real_dokploy_auth=True,
    )

    # Then
    assert updated.values["DOKPLOY_API_KEY"] == "reusable-key"
    assert env_file.read_text(encoding="utf-8") == (
        "DOKPLOY_API_KEY=reusable-key\n"
        "DOKPLOY_API_URL=http://127.0.0.1:3000\n"
        "DOKPLOY_BOOTSTRAP_MOCK_API_KEY=reusable-key\n"
        "DOKPLOY_MOCK_API_MODE=true\n"
        "ROOT_DOMAIN=example.test\n"
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
