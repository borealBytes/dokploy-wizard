from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from dokploy_wizard.packs.coder.reconciler import CoderError
from dokploy_wizard.proof import coder_workspace_create_preflight_cli as preflight_cli
from dokploy_wizard.proof import coder_workspace_create_preflight_cli_runner
from dokploy_wizard.proof import coder_workspace_create_preflight_setup as preflight_setup
from dokploy_wizard.proof.coder_workspace_create_preflight_cli import (
    _snapshot_failure,
    collect_preflight,
    main,
)
from dokploy_wizard.proof.coder_workspace_create_preflight_types import (
    CoderCreatePreflightError,
    CoderCreatePreflightReport,
    PreflightBlocker,
)
from dokploy_wizard.proof.model_sync_coder_api import CoderSnapshotApiError
from dokploy_wizard.proof.model_sync_env import EnvPreparationError
from dokploy_wizard.proof.model_sync_results import ProofTransport
from dokploy_wizard.proof.model_sync_task1_context import Task1ProofContextError
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextV1
from dokploy_wizard.state import RawEnvInput, StateValidationError


def _task1_context() -> Task1ProofContextV1:
    return Task1ProofContextV1(
        context_id="context",
        source_env_sha256="source",
        normalized_env_sha256="normalized",
        overlay_env_sha256="overlay",
        uploaded_env_sha256="uploaded",
        namespace_sha256="namespace",
        expected_restored_source_sha256="restored",
        source_env_mode=0o600,
        root_domain="example.test",
        stack_name="stack",
        tunnel_name="tunnel",
        dokploy_subdomain="dokploy",
        coder_subdomain="coder",
        seaweedfs_subdomain="seaweedfs",
        litellm_admin_subdomain="litellm",
    )


@pytest.mark.parametrize(
    ("stage", "blocker"),
    [
        ("container_missing", "coder_container_missing"),
        ("container_not_running", "coder_container_not_running"),
        ("container_restarting", "coder_container_restarting"),
        ("container_ambiguous", "coder_container_ambiguous"),
        ("container_discovery_unavailable", "coder_container_discovery_unavailable"),
        ("container_discovery_inconsistent", "coder_container_discovery_inconsistent"),
        ("inspect", "coder_container_inspect_unavailable"),
        ("network", "coder_shared_network_unavailable"),
        ("address", "coder_shared_address_invalid"),
        ("endpoint", "coder_url_unavailable"),
        ("payload", "preflight_payload_invalid"),
    ],
)
def test_snapshot_failure_projects_closed_stage(
    stage: Literal[
        "container_missing",
        "container_not_running",
        "container_restarting",
        "container_ambiguous",
        "container_discovery_unavailable",
        "container_discovery_inconsistent",
        "inspect",
        "network",
        "address",
        "endpoint",
        "payload",
    ],
    blocker: PreflightBlocker,
) -> None:
    # Given / When
    report = _snapshot_failure(CoderSnapshotApiError(stage))

    # Then
    assert report.blockers == (blocker,)
    assert CoderCreatePreflightReport.from_bytes(report.to_bytes()) == report
    assert str(CoderSnapshotApiError(stage)) == "Coder snapshot API is unavailable"


def test_coder_cli_does_not_expose_container_discovery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def unavailable(_service: str) -> str | None:
        raise CoderError("opaque")

    monkeypatch.setattr(
        coder_workspace_create_preflight_cli_runner,
        "_coder_container_name",
        unavailable,
    )

    # When / Then
    with pytest.raises(CoderCreatePreflightError) as error:
        coder_workspace_create_preflight_cli_runner.run_coder_cli("proof-stack", "token", ())

    assert "opaque" not in str(error.value)


@pytest.mark.parametrize(
    "failure",
    (StateValidationError("invalid env"), OSError("unreadable env")),
)
def test_collect_preflight_projects_env_parse_or_read_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: StateValidationError | OSError,
) -> None:
    # Given
    def invalid_env(_path: Path) -> RawEnvInput:
        raise failure

    monkeypatch.setattr(preflight_setup, "parse_env_file", invalid_env)

    # When
    report = collect_preflight(Path("env"), Path("context"))

    # Then
    assert report.blockers == ("preflight_env_unavailable",)
    assert CoderCreatePreflightReport.from_bytes(report.to_bytes()) == report
    assert str(failure).encode() not in report.to_bytes()


def test_collect_preflight_projects_task1_context_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def unavailable_context(_path: Path, _raw_env: RawEnvInput) -> Task1ProofContextV1:
        raise Task1ProofContextError("invalid context")

    monkeypatch.setattr(preflight_setup, "load_task1_proof_context", unavailable_context)
    monkeypatch.setattr(
        preflight_setup,
        "parse_env_file",
        lambda _path: RawEnvInput(format_version=1, values={"ROOT_DOMAIN": "example.test"}),
    )

    # When
    report = collect_preflight(Path("env"), Path("context"))

    # Then
    assert report.blockers == ("preflight_task1_context_unavailable",)
    assert CoderCreatePreflightReport.from_bytes(report.to_bytes()) == report
    assert b"invalid context" not in report.to_bytes()


def test_collect_preflight_projects_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def unavailable_transport(_path: Path) -> ProofTransport:
        raise EnvPreparationError("invalid transport")

    monkeypatch.setattr(preflight_setup, "resolve_proof_transport", unavailable_transport)
    monkeypatch.setattr(
        preflight_setup,
        "parse_env_file",
        lambda _path: RawEnvInput(format_version=1, values={"ROOT_DOMAIN": "example.test"}),
    )
    monkeypatch.setattr(
        preflight_setup,
        "load_task1_proof_context",
        lambda _path, _raw_env: _task1_context(),
    )

    # When
    report = collect_preflight(Path("env"), Path("context"))

    # Then
    assert report.blockers == ("preflight_transport_unavailable",)
    assert CoderCreatePreflightReport.from_bytes(report.to_bytes()) == report
    assert b"invalid transport" not in report.to_bytes()


def test_collect_preflight_keeps_malformed_transport_configuration_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    transport = ProofTransport(
        cloudflare_account_id=None,
        cloudflare_zone_id=None,
        cloudflare_zone_name="example.test",
        cloudflare_token=None,
        dokploy_api_url=None,
        dokploy_api_key=None,
        coder_email=None,
        coder_hostname=None,
        coder_password=None,
        tailscale_required=False,
    )
    monkeypatch.setattr(
        preflight_setup,
        "parse_env_file",
        lambda _path: RawEnvInput(format_version=1, values={"ROOT_DOMAIN": "example.test"}),
    )
    monkeypatch.setattr(
        preflight_setup,
        "load_task1_proof_context",
        lambda _path, _raw_env: _task1_context(),
    )
    monkeypatch.setattr(preflight_setup, "resolve_proof_transport", lambda _path: transport)

    # When
    report = collect_preflight(Path("env"), Path("context"))

    # Then
    assert report.blockers == ("preflight_transport_configuration_invalid",)
    assert CoderCreatePreflightReport.from_bytes(report.to_bytes()) == report


def test_main_projects_unexpected_top_level_preflight_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    def unexpected_preflight(_env: Path, _context: Path) -> CoderCreatePreflightReport:
        raise CoderCreatePreflightError("unexpected preflight failure")

    output = tmp_path / "report.json"
    monkeypatch.setattr(preflight_cli, "collect_preflight", unexpected_preflight)

    # When
    exit_code = main(
        (
            "--env-file",
            "env",
            "--task1-proof-context",
            "context",
            "--output",
            str(output),
        )
    )

    # Then
    report = CoderCreatePreflightReport.from_bytes(output.read_bytes())
    assert exit_code == 0
    assert report.blockers == ("preflight_setup_unexpected",)
    assert b"unexpected preflight failure" not in output.read_bytes()
