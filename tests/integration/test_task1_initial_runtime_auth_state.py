from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.proof.model_sync_task1_context import (
    activate_task1_proof_context,
    load_task1_proof_context,
)
from dokploy_wizard.state import RawEnvInput, load_state_dir, parse_env_file
from dokploy_wizard.state.dokploy_runtime_auth import (
    DokployRuntimeAuth,
    merge_dokploy_runtime_auth,
    persist_dokploy_runtime_auth,
)
from tests.helpers.task1_runtime_auth import prepare_task1_runtime_auth_fixture


class _BootstrapBackend:
    def is_healthy(self) -> bool:
        return True

    def install(self) -> None:
        raise AssertionError("install should not be called")


def test_task1_initial_install_preserves_upload_bound_raw_state_after_runtime_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    fixture = prepare_task1_runtime_auth_fixture(tmp_path)
    raw_env = parse_env_file(fixture.preparation.uploaded_env_file)
    state_dir = tmp_path / "state"
    runtime_auth = DokployRuntimeAuth(
        api_url="http://127.0.0.1:3000",
        api_key="generated-runtime-key",
    )
    monkeypatch.setattr(cli, "collect_host_facts", lambda _: object())
    monkeypatch.setattr(
        cli,
        "_prepare_install_host_prerequisites",
        lambda **kwargs: (kwargs["host_facts"], {}),
    )
    monkeypatch.setattr(cli, "run_preflight", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "_qualify_dokploy_mutation_auth", lambda **_: None)
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **_: {"state_status": "fresh"})
    monkeypatch.setattr(cli, "_build_coder_backend", lambda **_: object())

    def ensure_runtime_auth(**_kwargs: object) -> RawEnvInput:
        persist_dokploy_runtime_auth(state_dir, runtime_auth)
        return merge_dokploy_runtime_auth(raw_env, runtime_auth)

    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", ensure_runtime_auth)

    # When
    with activate_task1_proof_context(fixture.preparation.context):
        cli.run_install_flow(
            env_file=fixture.preparation.uploaded_env_file,
            state_dir=state_dir,
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=_BootstrapBackend(),
            networking_backend=object(),
        )

    # Then
    loaded_state = load_state_dir(state_dir)
    assert loaded_state.raw_input is not None
    assert loaded_state.desired_state is not None
    assert loaded_state.applied_state is not None
    assert loaded_state.raw_input == raw_env
    assert (
        load_task1_proof_context(fixture.preparation.context_file, loaded_state.raw_input)
        == fixture.preparation.context
    )
    assert (
        loaded_state.applied_state.desired_state_fingerprint
        == loaded_state.desired_state.fingerprint()
    )
