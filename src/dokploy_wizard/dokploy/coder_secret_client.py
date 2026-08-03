from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final
from uuid import UUID

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from dokploy_wizard.dokploy.coder_secret_reconciliation import (
    CoderSecretMetadata,
    CoderSecretSpec,
)
from dokploy_wizard.dokploy.coder_secret_types import (
    CoderSecretClientError,
    CoderSecretProcessRunner,
)
from dokploy_wizard.dokploy.coder_secret_workspace import CoderWorkspaceValueHashVerifier
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    CoderWorkspaceClock,
    SystemCoderWorkspaceClock,
    WorkspaceVerificationPolicy,
)
from dokploy_wizard.dokploy.coder_secret_workspace_inventory import workspace_records
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import WorkspaceVerificationReceipt

_MAX_OUTPUT_BYTES: Final = 64 * 1024
_COMMAND_TIMEOUT_SECONDS: Final = 60
_SECRET_METADATA_KEYS: Final = frozenset(
    ("id", "name", "env_name", "description", "created_at", "updated_at", "file_path")
)


@dataclass(frozen=True, slots=True)
class DockerExecCoderSecretClient:
    container_name: str
    session_token: str
    state_dir: Path = Path(".dokploy-wizard-state")
    runner: CoderSecretProcessRunner | None = None
    workspace_clock: CoderWorkspaceClock | None = None
    workspace_policy: WorkspaceVerificationPolicy = WorkspaceVerificationPolicy()
    workspace_name: str | None = None

    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
        return _parse_metadata_list(self._secret_run(("list", "--output", "json"), stdin=None))

    def write_secret(self, operation: str, spec: CoderSecretSpec) -> str:
        if operation not in {"create", "update"}:
            raise CoderSecretClientError("Coder secret operation is invalid")
        self._require_environment_binding(operation)
        output = self._secret_run(
            (operation, "--env", spec.env_name, "--description", spec.description, spec.name),
            stdin=spec.value,
        )
        return sha256(output.encode()).hexdigest()

    def delete_secret(self, secret: CoderSecretMetadata) -> None:
        self._secret_run(("delete", "--yes", secret.name), stdin=None)

    def require_workspace_receipt_absence(self, receipt: WorkspaceVerificationReceipt) -> None:
        if receipt.workspace_id is None:
            raise CoderSecretClientError("Coder verification workspace identity is unavailable")
        matches = tuple(
            workspace
            for workspace in workspace_records(self._coder_run(("list", "--output", "json")))
            if workspace.workspace_id == receipt.workspace_id
        )
        if not matches:
            return
        workspace = matches[0]
        if len(matches) != 1 or (
            workspace.workspace_name != receipt.workspace_name
            or workspace.owner_id != receipt.workspace_owner_id
            or workspace.owner_name != receipt.workspace_owner_name
            or workspace.template_id != receipt.template_id
            or workspace.template_name != receipt.template_name
        ):
            raise CoderSecretClientError("Coder verification workspace identity drifted")
        raise CoderSecretClientError("Coder verification workspace remains present")

    def verify_workspace_value_hash(self, spec: CoderSecretSpec, owner_id: str) -> str:
        verifier = CoderWorkspaceValueHashVerifier(
            runner=self._coder_run,
            state_dir=self.state_dir,
            clock=self.workspace_clock or SystemCoderWorkspaceClock(),
            policy=self.workspace_policy,
            workspace_name=self.workspace_name,
        )
        return verifier.verify(spec, owner_id)

    def _require_environment_binding(self, operation: str) -> None:
        help_text = self._secret_run((operation, "--help"), stdin=None)
        if "--env" not in help_text:
            raise CoderSecretClientError("Coder CLI lacks required secret environment binding")

    def _secret_run(self, command: tuple[str, ...], *, stdin: str | None) -> str:
        return self._run(("secret", *command), stdin=stdin)

    def _coder_run(self, command: tuple[str, ...]) -> str:
        return self._run(command, stdin=None)

    def _run(self, command: tuple[str, ...], *, stdin: str | None) -> str:
        runner = self.runner or _run_process
        try:
            result = runner(
                (
                    "docker",
                    "exec",
                    "-i",
                    "-e",
                    "CODER_URL=http://127.0.0.1:3000",
                    "-e",
                    "CODER_SESSION_TOKEN",
                    self.container_name,
                    "/opt/coder",
                    *command,
                ),
                input=stdin,
                check=False,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                env={**os.environ, "CODER_SESSION_TOKEN": self.session_token},
            )
        except subprocess.TimeoutExpired as error:
            raise CoderSecretClientError("Coder secret command timed out") from error
        if result.returncode != 0:
            raise CoderSecretClientError("Coder secret command failed")
        if (
            len(result.stdout.encode()) > _MAX_OUTPUT_BYTES
            or len(result.stderr.encode()) > _MAX_OUTPUT_BYTES
        ):
            raise CoderSecretClientError("Coder secret command output exceeds its limit")
        return result.stdout


def _run_process(
    arguments: tuple[str, ...],
    *,
    input: str | None,
    check: bool,
    capture_output: bool,
    text: bool,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        input=input,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


def _parse_metadata_list(output: str) -> tuple[CoderSecretMetadata, ...]:
    try:
        value: JsonValue = json.loads(output)
    except json.JSONDecodeError as error:
        raise CoderSecretClientError("Coder secret metadata is malformed") from error
    if not isinstance(value, list):
        raise CoderSecretClientError("Coder secret metadata must be a list")
    metadata = tuple(_parse_metadata(record) for record in value)
    names = tuple(item.name for item in metadata)
    identifiers = tuple(item.secret_id for item in metadata)
    if len(set(names)) != len(names) or len(set(identifiers)) != len(identifiers):
        raise CoderSecretClientError("Coder secret metadata is ambiguous")
    return metadata


def _parse_metadata(value: JsonValue) -> CoderSecretMetadata:
    if not isinstance(value, dict) or frozenset(value) != _SECRET_METADATA_KEYS:
        raise CoderSecretClientError("Coder secret metadata has an invalid schema")
    return CoderSecretMetadata(
        secret_id=_uuid(value.get("id"), "id"),
        name=_text(value.get("name"), "name"),
        env_name=_text(value.get("env_name"), "env_name"),
        description=_text(value.get("description"), "description"),
    )


def _uuid(value: JsonValue | None, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise CoderSecretClientError("Coder secret metadata ID is invalid") from error
    if str(parsed) != text:
        raise CoderSecretClientError("Coder secret metadata ID is invalid")
    return text


def _text(value: JsonValue | None, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise CoderSecretClientError(f"Coder secret metadata {label} is invalid")
    return value


__all__ = ["CoderSecretClientError", "DockerExecCoderSecretClient"]
