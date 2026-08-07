from __future__ import annotations

from typing import Literal

import pytest

from dokploy_wizard.proof.coder_workspace_create_preflight_cli import _snapshot_failure
from dokploy_wizard.proof.coder_workspace_create_preflight_types import PreflightBlocker
from dokploy_wizard.proof.model_sync_coder_api import CoderSnapshotApiError


@pytest.mark.parametrize(
    ("stage", "blocker"),
    [
        ("container", "coder_container_unavailable"),
        ("inspect", "coder_container_inspect_unavailable"),
        ("network", "coder_shared_network_unavailable"),
        ("address", "coder_shared_address_invalid"),
        ("endpoint", "coder_url_unavailable"),
        ("payload", "preflight_payload_invalid"),
    ],
)
def test_snapshot_failure_projects_closed_stage(
    stage: Literal["container", "inspect", "network", "address", "endpoint", "payload"],
    blocker: PreflightBlocker,
) -> None:
    # Given / When
    report = _snapshot_failure(CoderSnapshotApiError(stage))

    # Then
    assert report.blockers == (blocker,)
    assert str(CoderSnapshotApiError(stage)) == "Coder snapshot API is unavailable"
