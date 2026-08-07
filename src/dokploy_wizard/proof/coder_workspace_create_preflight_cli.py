"""Run the remote read-only Coder workspace-create preflight."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, Sequence, assert_never

from dokploy_wizard.dokploy.coder import _coder_container_name
from dokploy_wizard.proof.coder_workspace_create_preflight import (
    classify_preflight,
    target_active_version_id,
)
from dokploy_wizard.proof.coder_workspace_create_preflight_types import (
    AuthStatus,
    CoderCreatePreflightError,
    CoderCreatePreflightReport,
    PreflightBlocker,
    TokenStatus,
    UrlStatus,
)
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_coder_api import (
    CoderSnapshotApiError,
    api,
    coder_login,
)
from dokploy_wizard.proof.model_sync_env import EnvPreparationError, resolve_proof_transport
from dokploy_wizard.proof.model_sync_task1_context import (
    Task1ProofContextError,
    activate_task1_proof_context,
    load_task1_proof_context,
)
from dokploy_wizard.state import StateValidationError, parse_env_file

_OUTPUT_LIMIT: Final = 2 * 1024 * 1024
_COMMAND_TIMEOUT_SECONDS: Final = 60
PreflightFailure = Literal[
    "coder_url_unavailable",
    "coder_token_unavailable",
    "coder_auth_unavailable",
    "preflight_transport_configuration_invalid",
    "preflight_payload_invalid",
]


@dataclass(frozen=True, slots=True)
class _UnavailableReport:
    url: UrlStatus
    token: TokenStatus
    auth: AuthStatus
    blocker: PreflightBlocker


def main(argv: Sequence[str] | None = None) -> int:
    """Write only a closed, private preflight result to the requested remote path."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--task1-proof-context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = collect_preflight(args.env_file, args.task1_proof_context)
    except (
        CoderCreatePreflightError,
        EnvPreparationError,
        OSError,
        StateValidationError,
        Task1ProofContextError,
    ):
        report = _unavailable_report("preflight_transport_configuration_invalid")
    _write_private_report(args.output, report)
    return 0


def collect_preflight(env_file: Path, context_file: Path) -> CoderCreatePreflightReport:
    """Collect read-only Coder contract state without exposing provider response values."""

    raw_env = parse_env_file(env_file)
    context = load_task1_proof_context(context_file, raw_env)
    transport = resolve_proof_transport(env_file)
    if (
        transport.coder_email is None
        or transport.coder_hostname is None
        or transport.coder_password is None
    ):
        return _unavailable_report("coder_token_unavailable")
    with activate_task1_proof_context(context):
        try:
            api(transport.coder_hostname, None, "/api/v2/buildinfo")
        except (CoderSnapshotApiError, OSError, ValueError):
            return _unavailable_report("coder_url_unavailable")
        try:
            token = coder_login(
                transport.coder_hostname,
                transport.coder_email,
                transport.coder_password,
            )
        except (CoderSnapshotApiError, OSError, ValueError):
            return _unavailable_report("coder_token_unavailable")
        try:
            templates = _run_coder_cli(
                context.stack_name,
                token,
                ("templates", "list", "--output", "json"),
            )
        except CoderCreatePreflightError:
            return _unavailable_report("coder_auth_unavailable")
        try:
            version_id = target_active_version_id(templates)
        except CoderCreatePreflightError:
            return _unavailable_report("preflight_payload_invalid")
        if version_id is None:
            try:
                report = classify_preflight(templates, None, None, None, None)
            except CoderCreatePreflightError:
                report = _unavailable_report("preflight_payload_invalid")
            return replace(
                report,
                url_status="reachable",
                token_status="issued",
                auth_status="authenticated",
            )
        try:
            version = api(transport.coder_hostname, token, f"/api/v2/templateversions/{version_id}")
            parameters = api(
                transport.coder_hostname,
                token,
                f"/api/v2/templateversions/{version_id}/rich-parameters",
            )
            presets = api(
                transport.coder_hostname,
                token,
                f"/api/v2/templateversions/{version_id}/presets",
            )
            external_auth = api(
                transport.coder_hostname,
                token,
                f"/api/v2/templateversions/{version_id}/external-auth",
            )
        except (CoderSnapshotApiError, OSError, ValueError):
            version = None
            parameters = None
            presets = None
            external_auth = None
        try:
            report = classify_preflight(templates, version, parameters, presets, external_auth)
        except CoderCreatePreflightError:
            report = _unavailable_report("coder_auth_unavailable")
        return replace(
            report,
            url_status="reachable",
            token_status="issued",
            auth_status="authenticated",
        )


def _run_coder_cli(stack_name: str, token: str, arguments: tuple[str, ...]) -> JsonValue:
    container = _coder_container_name(f"{stack_name}-coder")
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


def _unavailable_report(blocker: PreflightFailure) -> CoderCreatePreflightReport:
    match blocker:
        case "coder_url_unavailable":
            unavailable = _UnavailableReport("unavailable", "not_checked", "not_checked", blocker)
        case "coder_token_unavailable":
            unavailable = _UnavailableReport("reachable", "unavailable", "not_checked", blocker)
        case "coder_auth_unavailable":
            unavailable = _UnavailableReport("reachable", "issued", "unavailable", blocker)
        case "preflight_transport_configuration_invalid":
            unavailable = _UnavailableReport("not_checked", "not_checked", "not_checked", blocker)
        case "preflight_payload_invalid":
            unavailable = _UnavailableReport("reachable", "issued", "authenticated", blocker)
        case unreachable:
            assert_never(unreachable)
    return CoderCreatePreflightReport(
        url_status=unavailable.url,
        token_status=unavailable.token,
        auth_status=unavailable.auth,
        target_template_count=0,
        organization_count=0,
        target_organization_count=0,
        active_template_version_health="not_checked",
        preset_selection="not_checked",
        required_parameter_default_gap_count=0,
        required_external_auth_unsatisfied_count=0,
        external_auth_status="not_checked",
        blockers=(unavailable.blocker,),
    )


def _write_private_report(output: Path, report: CoderCreatePreflightReport) -> None:
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(report.to_bytes())
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    raise SystemExit(main())
