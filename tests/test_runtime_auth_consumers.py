from __future__ import annotations

import stat
from pathlib import Path
from typing import TypedDict, Unpack

import pytest

from dokploy_wizard import cli
from dokploy_wizard.core.planner import build_pack_env_specs
from dokploy_wizard.dokploy.env_spec import DokployEnvSpec
from dokploy_wizard.service_verification_runner import run_service_verification
from dokploy_wizard.state import (
    DesiredState,
    LiteLLMGeneratedKeys,
    RawEnvInput,
    StateValidationError,
)
from dokploy_wizard.state.dokploy_runtime_auth import (
    DokployRuntimeAuth,
    persist_dokploy_runtime_auth,
)
from dokploy_wizard.verification import ServiceVerificationResult
from tests.helpers.task1_runtime_auth import prepare_task1_runtime_auth_fixture

_API_URL = "http://127.0.0.1:3000"
_API_KEY = "generated-runtime-key"


class _SharedCoreBuilderKwargs(TypedDict):
    raw_env: RawEnvInput
    state_dir: Path
    desired_state: DesiredState
    session_client: None
    litellm_generated_keys: LiteLLMGeneratedKeys | None


def test_service_verification_uses_state_auth_without_mutating_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    fixture = prepare_task1_runtime_auth_fixture(tmp_path)
    upload_path = fixture.preparation.uploaded_env_file
    state_dir = tmp_path / "state"
    persist_dokploy_runtime_auth(
        state_dir,
        DokployRuntimeAuth(api_url=_API_URL, api_key=_API_KEY),
    )
    seen: dict[str, str] = {}

    def build_shared_core_backend(
        **kwargs: Unpack[_SharedCoreBuilderKwargs],
    ) -> None:
        raw_env = kwargs["raw_env"]
        seen["api_url"] = raw_env.values["DOKPLOY_API_URL"]
        seen["api_key"] = raw_env.values["DOKPLOY_API_KEY"]
        return None

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_shared_core_backend",
        build_shared_core_backend,
    )
    for builder_name in (
        "_build_nextcloud_backend",
        "_build_moodle_backend",
        "_build_docuseal_backend",
        "_build_seaweedfs_backend",
        "_build_coder_backend",
        "_build_openclaw_backend",
        "_build_surfsense_backend",
    ):
        monkeypatch.setattr(
            f"dokploy_wizard.service_verification_runner.cli.{builder_name}",
            lambda **_: None,
        )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_dokploy_session_client",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner._verify_shared_core",
        lambda **_: ServiceVerificationResult(
            service_name="shared-core",
            tier="app",
            status="pass",
            detail="verified",
        ),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner._verify_backend_method",
        lambda **_: ServiceVerificationResult(
            service_name="coder",
            tier="app",
            status="pass",
            detail="verified",
        ),
    )

    # When
    payload = run_service_verification(
        env_file=upload_path,
        state_dir=state_dir,
        task1_proof_context=fixture.preparation.context_file,
    )

    # Then
    assert payload["passed"] is True
    assert seen == {"api_url": _API_URL, "api_key": _API_KEY}
    assert upload_path.read_bytes() == fixture.upload_bytes
    assert stat.S_IMODE(upload_path.stat().st_mode) == fixture.upload_mode


def test_service_verification_fails_closed_for_wrong_mode_runtime_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    fixture = prepare_task1_runtime_auth_fixture(tmp_path)
    state_dir = tmp_path / "state"
    persist_dokploy_runtime_auth(
        state_dir,
        DokployRuntimeAuth(api_url=_API_URL, api_key=_API_KEY),
    )
    (state_dir / "dokploy-runtime-auth.json").chmod(0o640)
    backend_called = False

    def reject_backend_call(**_kwargs: Unpack[_SharedCoreBuilderKwargs]) -> None:
        nonlocal backend_called
        backend_called = True
        return None

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_shared_core_backend",
        reject_backend_call,
    )

    # When
    with pytest.raises(StateValidationError) as raised:
        run_service_verification(
            env_file=fixture.preparation.uploaded_env_file,
            state_dir=state_dir,
            task1_proof_context=fixture.preparation.context_file,
        )

    # Then
    assert backend_called is False
    assert _API_KEY not in str(raised.value)


def test_inspect_state_command_merges_state_auth_without_mutating_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    fixture = prepare_task1_runtime_auth_fixture(tmp_path)
    upload_path = fixture.preparation.uploaded_env_file
    state_dir = tmp_path / "state"
    persist_dokploy_runtime_auth(
        state_dir,
        DokployRuntimeAuth(api_url=_API_URL, api_key=_API_KEY),
    )
    seen: dict[str, str] = {}

    def capture_pack_env_specs(
        stack_name: str,
        enabled_packs: tuple[str, ...],
        values: dict[str, str],
    ) -> tuple[DokployEnvSpec, ...]:
        seen["api_url"] = values["DOKPLOY_API_URL"]
        seen["api_key"] = values["DOKPLOY_API_KEY"]
        return build_pack_env_specs(stack_name, enabled_packs, values)

    monkeypatch.setattr(cli, "build_pack_env_specs", capture_pack_env_specs)
    monkeypatch.setattr(cli, "build_live_drift_report", lambda **_: {"status": "clean"})

    # When
    exit_code = cli.main(
        [
            "inspect-state",
            "--env-file",
            str(upload_path),
            "--state-dir",
            str(state_dir),
            "--task1-proof-context",
            str(fixture.preparation.context_file),
            "--dry-run",
        ]
    )

    # Then
    output = capsys.readouterr().out
    assert exit_code == 0
    assert seen == {"api_url": _API_URL, "api_key": _API_KEY}
    assert _API_KEY not in output
    assert upload_path.read_bytes() == fixture.upload_bytes
    assert stat.S_IMODE(upload_path.stat().st_mode) == fixture.upload_mode
