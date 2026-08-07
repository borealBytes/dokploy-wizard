from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_client import (
    CoderSecretClientError,
    DockerExecCoderSecretClient,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation import (
    CoderSecretMetadata,
    CoderSecretSpec,
)
from dokploy_wizard.dokploy.coder_secret_workspace_inventory import verification_template


class RecordingRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self._results = results

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        input: str | None,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert capture_output is True
        assert text is True
        assert timeout == 60
        assert env is not None
        assert "CODER_SESSION_TOKEN" in env
        self.calls.append((arguments, input))
        return self._results.pop(0)


@pytest.mark.parametrize(
    ("payload", "expected_kind"),
    (
        ("{", "client_workspace_template_payload"),
        ("{}", "client_workspace_template_root"),
        (
            '[{"id":"00000000-0000-4000-8000-000000000001","name":"retired"}]',
            "client_workspace_template_primary_absent",
        ),
        (
            '[{"id":"00000000-0000-4000-8000-000000000001",'
            '"name":"ubuntu-vscode-opencode-pi"},{"id":'
            '"00000000-0000-4000-8000-000000000002",'
            '"name":"ubuntu-vscode-opencode-pi"}]',
            "client_workspace_template_primary_ambiguous",
        ),
        (
            '[{"id":"00000000-0000-4000-8000-000000000001",'
            '"name":"ubuntu-vscode"},{"id":'
            '"00000000-0000-4000-8000-000000000002","name":"ubuntu-vscode"}]',
            "client_workspace_template_legacy_ambiguous",
        ),
        (
            '[{"id":"invalid","name":"ubuntu-vscode-opencode-pi"}]',
            "client_workspace_template_record_invalid",
        ),
    ),
)
def test_verification_template_failure_origin_is_value_free(
    payload: str,
    expected_kind: str,
) -> None:
    with pytest.raises(CoderSecretClientError) as raised:
        verification_template(payload)

    assert raised.value.kind == expected_kind
    assert "00000000" not in str(raised.value)


def test_secret_client_probes_environment_binding_before_stdin_write() -> None:
    value = "SECRET-CODER-HERMES"
    runner = RecordingRunner(
        [
            subprocess.CompletedProcess((), 0, stdout="  --env string\n", stderr=""),
            subprocess.CompletedProcess((), 0, stdout="created\n", stderr=""),
        ]
    )
    client = DockerExecCoderSecretClient(
        container_name="coder-container", session_token="session-token", runner=runner
    )
    spec = CoderSecretSpec(
        name="hermes-openai-api-key",
        env_name="OPENAI_API_KEY",
        value=value,
        description="Hermes LiteLLM key",
    )

    response_hash = client.write_secret("create", spec)

    assert len(response_hash) == 64
    assert runner.calls[0][0][-4:] == ("/opt/coder", "secret", "create", "--help")
    assert runner.calls[1][0][-6:] == (
        "create",
        "--env",
        "OPENAI_API_KEY",
        "--description",
        "Hermes LiteLLM key",
        "hermes-openai-api-key",
    )
    assert runner.calls[1][1] == value
    assert all(value not in argument for call, _ in runner.calls for argument in call)


@pytest.mark.parametrize(
    "payload",
    (
        [
            {
                "id": "not-a-uuid",
                "name": "x",
                "env_name": "X",
                "description": "x",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "file_path": "",
            }
        ],
        [
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "name": "x",
                "description": "x",
            }
        ],
        [
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "name": "x",
                "env_name": "X",
                "description": "x",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "file_path": "",
            },
            {
                "id": "00000000-0000-4000-8000-000000000002",
                "name": "x",
                "env_name": "Y",
                "description": "y",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "file_path": "",
            },
        ],
    ),
)
def test_secret_client_rejects_malformed_or_ambiguous_metadata(
    payload: list[dict[str, str]],
) -> None:
    runner = RecordingRunner(
        [subprocess.CompletedProcess((), 0, stdout=json.dumps(payload), stderr="")]
    )
    client = DockerExecCoderSecretClient(
        container_name="coder-container", session_token="session-token", runner=runner
    )

    with pytest.raises(CoderSecretClientError) as raised:
        client.list_secrets()

    assert raised.value.kind == "client_metadata_invalid"


def test_secret_client_metadata_uses_closed_schema() -> None:
    payload = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "hermes-model",
            "env_name": "HERMES_MODEL",
            "description": "Hermes model",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "file_path": "",
        }
    ]
    runner = RecordingRunner(
        [subprocess.CompletedProcess((), 0, stdout=json.dumps(payload), stderr="")]
    )
    client = DockerExecCoderSecretClient(
        container_name="coder-container", session_token="session-token", runner=runner
    )

    assert client.list_secrets() == (
        CoderSecretMetadata(
            secret_id="00000000-0000-4000-8000-000000000001",
            name="hermes-model",
            env_name="HERMES_MODEL",
            description="Hermes model",
        ),
    )


def test_secret_client_rejects_unsupported_environment_binding_before_write() -> None:
    value = "SECRET-CODER-KDENSE"
    runner = RecordingRunner(
        [subprocess.CompletedProcess((), 0, stdout="Usage: coder secret create\n", stderr="")]
    )
    client = DockerExecCoderSecretClient(
        container_name="coder-container", session_token="session-token", runner=runner
    )
    spec = CoderSecretSpec(
        name="kdense-litellm-api-key",
        env_name="KDENSE_LITELLM_API_KEY",
        value=value,
        description="K-Dense LiteLLM key",
    )

    with pytest.raises(CoderSecretClientError) as raised:
        client.write_secret("create", spec)

    assert raised.value.kind == "client_env_binding"
    assert value not in str(raised.value)
    assert len(runner.calls) == 1
    assert all(value not in argument for call, _ in runner.calls for argument in call)


def test_secret_client_command_failure_keeps_fixed_origin_without_output() -> None:
    runner = RecordingRunner(
        [
            subprocess.CompletedProcess(
                (),
                1,
                stdout="provider output must be discarded",
                stderr="provider error must be discarded",
            )
        ]
    )
    client = DockerExecCoderSecretClient(
        container_name="coder-container", session_token="session-token", runner=runner
    )

    with pytest.raises(CoderSecretClientError) as raised:
        client.list_secrets()

    assert raised.value.kind == "client_command_failed"
    assert "provider" not in str(raised.value)


def test_secret_client_checks_update_environment_binding_before_write() -> None:
    value = "SECRET-CODER-HERMES"
    runner = RecordingRunner(
        [subprocess.CompletedProcess((), 0, stdout="Usage: coder secret update\n", stderr="")]
    )
    client = DockerExecCoderSecretClient(
        container_name="coder-container", session_token="session-token", runner=runner
    )
    spec = CoderSecretSpec(
        name="hermes-openai-api-key",
        env_name="OPENAI_API_KEY",
        value=value,
        description="Hermes LiteLLM key",
    )

    with pytest.raises(CoderSecretClientError):
        client.write_secret("update", spec)

    assert runner.calls[0][0][-4:] == ("/opt/coder", "secret", "update", "--help")
    assert all(value not in argument for call, _ in runner.calls for argument in call)


@pytest.mark.parametrize(
    "template_name", ("ubuntu-vscode-opencode-pi", "ubuntu-vscode")
)
def test_secret_client_observes_environment_hash_in_temporary_workspace(
    tmp_path: Path,
    template_name: str,
) -> None:
    expected_hash = sha256("SECRET-CODER-HERMES".encode()).hexdigest()
    runner = RecordingRunner(
        [
            subprocess.CompletedProcess(
                (),
                0,
                stdout=(
                    '[{"id":"00000000-0000-4000-8000-000000000003",'
                    f'"name":"{template_name}"}}]'
                ),
                stderr="",
            ),
            subprocess.CompletedProcess((), 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                (),
                0,
                stdout=(
                    '[{"id":"00000000-0000-4000-8000-000000000001",'
                    '"name":"proof-workspace",'
                    '"owner_id":"00000000-0000-4000-8000-000000000002",'
                    '"owner_name":"admin",'
                    '"template_id":"00000000-0000-4000-8000-000000000003",'
                    f'"template_name":"{template_name}",'
                    '"latest_build":{"status":"running"}}]'
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                (),
                0,
                stdout=(
                    '[{"id":"00000000-0000-4000-8000-000000000001",'
                    '"name":"proof-workspace",'
                    '"owner_id":"00000000-0000-4000-8000-000000000002",'
                    '"owner_name":"admin",'
                    '"template_id":"00000000-0000-4000-8000-000000000003",'
                    f'"template_name":"{template_name}",'
                    '"latest_build":{"status":"running"}}]'
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                (),
                0,
                stdout=(
                    '[{"id":"00000000-0000-4000-8000-000000000001",'
                    '"name":"proof-workspace",'
                    '"owner_id":"00000000-0000-4000-8000-000000000002",'
                    '"owner_name":"admin",'
                    '"template_id":"00000000-0000-4000-8000-000000000003",'
                    f'"template_name":"{template_name}",'
                    '"latest_build":{"status":"running"}}]'
                ),
                stderr="",
            ),
            subprocess.CompletedProcess((), 0, stdout=f"{expected_hash}\n", stderr=""),
            subprocess.CompletedProcess(
                (),
                0,
                stdout=(
                    '[{"id":"00000000-0000-4000-8000-000000000001",'
                    '"name":"proof-workspace",'
                    '"owner_id":"00000000-0000-4000-8000-000000000002",'
                    '"owner_name":"admin",'
                    '"template_id":"00000000-0000-4000-8000-000000000003",'
                    f'"template_name":"{template_name}",'
                    '"latest_build":{"status":"running"}}]'
                ),
                stderr="",
            ),
            subprocess.CompletedProcess((), 0, stdout="", stderr=""),
            subprocess.CompletedProcess((), 0, stdout="[]", stderr=""),
        ]
    )
    client = DockerExecCoderSecretClient(
        container_name="coder-container",
        session_token="session-token",
        state_dir=tmp_path,
        runner=runner,
        workspace_name="proof-workspace",
    )
    spec = CoderSecretSpec(
        name="hermes-openai-api-key",
        env_name="OPENAI_API_KEY",
        value="SECRET-CODER-HERMES",
        description="Hermes LiteLLM key",
    )
    observed_hash = client.verify_workspace_value_hash(spec, "a" * 64)

    assert observed_hash == expected_hash
    commands = [call[0] for call in runner.calls]
    assert any(
        command[-5:] == ("/opt/coder", "templates", "list", "--output", "json")
        for command in commands
    )
    assert any("create" in command for command in commands)
    assert any("ssh" in command for command in commands)
    assert any("delete" in command for command in commands)
    assert all(spec.value not in argument for call, _ in runner.calls for argument in call)
