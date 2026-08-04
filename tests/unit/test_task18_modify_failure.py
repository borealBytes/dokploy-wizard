from __future__ import annotations

import pytest

from dokploy_wizard.bootstrap import DokployBootstrapError
from dokploy_wizard.core import SharedCoreError
from dokploy_wizard.lifecycle import DriftReport, LifecycleDriftError
from dokploy_wizard.networking import CloudflareError
from dokploy_wizard.packs.coder import CoderError
from dokploy_wizard.packs.headscale import HeadscaleError
from dokploy_wizard.packs.matrix import MatrixError
from dokploy_wizard.packs.nextcloud import NextcloudError
from dokploy_wizard.packs.openclaw import OpenClawError
from dokploy_wizard.packs.seaweedfs import SeaweedFsError
from dokploy_wizard.preflight import PreflightError
from dokploy_wizard.proof.model_sync_task18_modify_failure import task18_modify_failure
from dokploy_wizard.state.models import StateValidationError
from dokploy_wizard.tailscale import TailscaleError


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (OSError("fixture"), "os_error"),
        (StateValidationError("fixture"), "state_validation"),
        (PreflightError("fixture"), "preflight"),
        (DokployBootstrapError("fixture"), "bootstrap"),
        (CloudflareError("fixture"), "cloudflare"),
        (SharedCoreError("fixture"), "shared_core"),
        (TailscaleError("fixture"), "tailscale"),
        (HeadscaleError("fixture"), "headscale"),
        (CoderError("fixture"), "coder"),
        (LifecycleDriftError("fixture", report=DriftReport(entries=())), "lifecycle_drift"),
        (MatrixError("fixture"), "matrix"),
        (NextcloudError("fixture"), "nextcloud"),
        (OpenClawError("fixture"), "openclaw"),
        (SeaweedFsError("fixture"), "seaweedfs"),
    ),
)
def test_task18_modify_failure_returns_only_exception_type_category(
    failure: BaseException,
    expected: str,
) -> None:
    # Given / When
    category = task18_modify_failure(failure)

    # Then
    assert category == expected
