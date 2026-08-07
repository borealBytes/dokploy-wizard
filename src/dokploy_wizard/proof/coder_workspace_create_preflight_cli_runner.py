"""Bounded Coder CLI reader used by the workspace-create preflight."""

from __future__ import annotations

import json
import subprocess
from typing import Final

from dokploy_wizard.dokploy.coder import _coder_container_name
from dokploy_wizard.packs.coder.reconciler import CoderError
from dokploy_wizard.proof.coder_workspace_create_preflight_types import CoderCreatePreflightError
from dokploy_wizard.proof.model_sync_artifacts import JsonValue

_OUTPUT_LIMIT: Final = 2 * 1024 * 1024
_COMMAND_TIMEOUT_SECONDS: Final = 60


def run_coder_cli(stack_name: str, token: str, arguments: tuple[str, ...]) -> JsonValue:
    try:
        container = _coder_container_name(f"{stack_name}-coder")
    except CoderError as error:
        raise CoderCreatePreflightError("Coder container is unavailable") from error
    if container is None:
        raise CoderCreatePreflightError("Coder container is unavailable")
    command = (
        "docker",
        "exec",
        "-i",
        "-e",
        "CODER_URL=http://127.0.0.1:3000",
        container,
        "sh",
        "-c",
        "IFS= read -r CODER_SESSION_TOKEN; export CODER_SESSION_TOKEN; exec /opt/coder \"$@\"",
        "sh",
        *arguments,
    )
    try:
        result = subprocess.run(
            command,
            input=f"{token}\n",
            check=False,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CoderCreatePreflightError("Coder CLI probe failed") from error
    if result.returncode != 0:
        raise CoderCreatePreflightError("Coder CLI probe failed")
    if len(result.stdout.encode()) > _OUTPUT_LIMIT or len(result.stderr.encode()) > _OUTPUT_LIMIT:
        raise CoderCreatePreflightError("Coder CLI output exceeded the capture limit")
    try:
        value: JsonValue = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CoderCreatePreflightError("Coder CLI output is invalid") from error
    return value
