"""Run the remote read-only Coder workspace-create preflight."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Sequence, assert_never

from dokploy_wizard.proof.coder_workspace_create_preflight import (
    classify_preflight,
    target_active_version_id,
)
from dokploy_wizard.proof.coder_workspace_create_preflight_cli_runner import (
    run_coder_cli as _run_coder_cli,
)
from dokploy_wizard.proof.coder_workspace_create_preflight_setup import (
    PreflightSetupBlocked,
    PreflightSetupReady,
    collect_preflight_setup,
)
from dokploy_wizard.proof.coder_workspace_create_preflight_types import (
    AuthStatus,
    CoderCreatePreflightError,
    CoderCreatePreflightReport,
    PreflightBlocker,
    TokenStatus,
    UrlStatus,
)
from dokploy_wizard.proof.model_sync_coder_api import (
    CoderSnapshotApiError,
    api,
    coder_login,
    nullable_api,
)
from dokploy_wizard.proof.model_sync_task1_context import (
    activate_task1_proof_context,
)

PreflightFailure = Literal[
    "coder_url_unavailable",
    "coder_token_unavailable",
    "coder_auth_unavailable",
    "preflight_env_unavailable",
    "preflight_task1_context_unavailable",
    "preflight_transport_unavailable",
    "preflight_setup_unexpected",
    "preflight_transport_configuration_invalid",
    "preflight_payload_invalid",
    "coder_container_missing",
    "coder_container_not_running",
    "coder_container_restarting",
    "coder_container_ambiguous",
    "coder_container_discovery_unavailable",
    "coder_container_discovery_inconsistent",
    "coder_container_inspect_unavailable",
    "coder_shared_network_unavailable",
    "coder_shared_address_invalid",
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
    except CoderCreatePreflightError:
        report = _unavailable_report("preflight_setup_unexpected")
    _write_private_report(args.output, report)
    return 0


def collect_preflight(env_file: Path, context_file: Path) -> CoderCreatePreflightReport:
    """Collect read-only Coder contract state without exposing provider response values."""

    setup = collect_preflight_setup(env_file, context_file)
    match setup:
        case PreflightSetupBlocked(blocker=blocker):
            return _unavailable_report(blocker)
        case PreflightSetupReady(
            context=context,
            transport=transport,
            coder_hostname=coder_hostname,
        ):
            pass
        case unreachable:
            assert_never(unreachable)
    with activate_task1_proof_context(context):
        try:
            api(coder_hostname, None, "/api/v2/buildinfo")
        except CoderSnapshotApiError as error:
            return _snapshot_failure(error)
        except (OSError, ValueError):
            return _unavailable_report("coder_url_unavailable")
        if transport.coder_email is None or transport.coder_password is None:
            return _unavailable_report("coder_token_unavailable")
        try:
            token = coder_login(
                coder_hostname,
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
            version = api(coder_hostname, token, f"/api/v2/templateversions/{version_id}")
            parameters = api(
                coder_hostname,
                token,
                f"/api/v2/templateversions/{version_id}/rich-parameters",
            )
            presets = nullable_api(
                coder_hostname,
                token,
                f"/api/v2/templateversions/{version_id}/presets",
            )
            external_auth = api(
                coder_hostname,
                token,
                f"/api/v2/templateversions/{version_id}/external-auth",
            )
        except CoderSnapshotApiError as error:
            return _snapshot_failure(error)
        except (OSError, ValueError):
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


def _unavailable_report(blocker: PreflightFailure) -> CoderCreatePreflightReport:
    match blocker:
        case "coder_url_unavailable":
            unavailable = _UnavailableReport("unavailable", "not_checked", "not_checked", blocker)
        case "coder_token_unavailable":
            unavailable = _UnavailableReport("reachable", "unavailable", "not_checked", blocker)
        case "coder_auth_unavailable":
            unavailable = _UnavailableReport("reachable", "issued", "unavailable", blocker)
        case (
            "preflight_env_unavailable"
            | "preflight_task1_context_unavailable"
            | "preflight_transport_unavailable"
            | "preflight_setup_unexpected"
            | "preflight_transport_configuration_invalid"
        ):
            unavailable = _UnavailableReport("not_checked", "not_checked", "not_checked", blocker)
        case "preflight_payload_invalid":
            unavailable = _UnavailableReport("reachable", "issued", "authenticated", blocker)
        case (
            "coder_container_missing"
            | "coder_container_not_running"
            | "coder_container_restarting"
            | "coder_container_ambiguous"
            | "coder_container_discovery_unavailable"
            | "coder_container_discovery_inconsistent"
            | "coder_container_inspect_unavailable"
            | "coder_shared_network_unavailable"
            | "coder_shared_address_invalid"
        ):
            unavailable = _UnavailableReport("not_checked", "not_checked", "not_checked", blocker)
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


def _snapshot_failure(error: CoderSnapshotApiError) -> CoderCreatePreflightReport:
    match error.stage:
        case "container_missing":
            return _unavailable_report("coder_container_missing")
        case "container_not_running":
            return _unavailable_report("coder_container_not_running")
        case "container_restarting":
            return _unavailable_report("coder_container_restarting")
        case "container_ambiguous":
            return _unavailable_report("coder_container_ambiguous")
        case "container_discovery_unavailable":
            return _unavailable_report("coder_container_discovery_unavailable")
        case "container_discovery_inconsistent":
            return _unavailable_report("coder_container_discovery_inconsistent")
        case "inspect":
            return _unavailable_report("coder_container_inspect_unavailable")
        case "network":
            return _unavailable_report("coder_shared_network_unavailable")
        case "address":
            return _unavailable_report("coder_shared_address_invalid")
        case "endpoint":
            return _unavailable_report("coder_url_unavailable")
        case "payload":
            return _unavailable_report("preflight_payload_invalid")
        case unreachable:
            assert_never(unreachable)


def _write_private_report(output: Path, report: CoderCreatePreflightReport) -> None:
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(report.to_bytes())
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    raise SystemExit(main())
