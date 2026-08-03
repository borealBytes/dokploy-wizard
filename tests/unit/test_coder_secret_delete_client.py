from __future__ import annotations

import subprocess
from collections.abc import Mapping

from dokploy_wizard.dokploy.coder_secret_client import DockerExecCoderSecretClient
from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretMetadata


class MetadataRunner:
    def __init__(self) -> None:
        self.arguments: tuple[str, ...] = ()
        self.environment_names: frozenset[str] = frozenset()

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
        del input, check, capture_output, text, timeout
        self.arguments = arguments
        self.environment_names = frozenset(() if env is None else env)
        return subprocess.CompletedProcess(arguments, 0, stdout="[]", stderr="")


def test_secret_client_keeps_session_token_out_of_command_arguments() -> None:
    runner = MetadataRunner()
    client = DockerExecCoderSecretClient(
        container_name="coder-container",
        session_token="opaque-token-input",
        runner=runner,
    )

    client.list_secrets()

    assert "CODER_SESSION_TOKEN" in runner.arguments
    assert not any(argument.startswith("CODER_SESSION_TOKEN=") for argument in runner.arguments)
    assert "CODER_SESSION_TOKEN" in runner.environment_names


def test_secret_client_delete_command_contains_metadata_name_but_no_value() -> None:
    runner = MetadataRunner()
    client = DockerExecCoderSecretClient(
        container_name="coder-container",
        session_token="opaque-token-input",
        runner=runner,
    )
    secret = CoderSecretMetadata(
        secret_id="00000000-0000-4000-8000-000000000001",
        name="hermes-model",
        env_name="HERMES_MODEL",
        description="Hermes model",
    )

    client.delete_secret(secret)

    assert runner.arguments[-4:] == ("secret", "delete", "--yes", secret.name)
    assert not any(argument.startswith("CODER_SESSION_TOKEN=") for argument in runner.arguments)
