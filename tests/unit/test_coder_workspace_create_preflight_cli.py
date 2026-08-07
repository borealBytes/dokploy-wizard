from __future__ import annotations

from typing import Literal

import pytest

from dokploy_wizard.packs.coder.reconciler import CoderError
from dokploy_wizard.proof import coder_workspace_create_preflight_cli_runner
from dokploy_wizard.proof.coder_workspace_create_preflight_cli import _snapshot_failure
from dokploy_wizard.proof.coder_workspace_create_preflight_types import (
    CoderCreatePreflightError,
    CoderCreatePreflightReport,
    PreflightBlocker,
)
from dokploy_wizard.proof.model_sync_coder_api import CoderSnapshotApiError


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
