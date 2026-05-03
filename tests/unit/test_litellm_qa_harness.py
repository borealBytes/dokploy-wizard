# mypy: ignore-errors
# ruff: noqa: E501
from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from urllib import error

import pytest

from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.litellm.qa_harness import (
    LiteLLMAdminAccessCheckError,
    build_litellm_admin_qa_harness,
    verify_public_litellm_admin_access,
)
from dokploy_wizard.state import DesiredState, RawEnvInput


@dataclass
class _FakeResponse:
    status: int

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        del exc_type, exc, tb
        return False

    def read(self) -> bytes:
        return b""


def test_build_litellm_admin_qa_harness_requires_access_challenge_and_internal_probe() -> None:
    harness = build_litellm_admin_qa_harness(
        raw_env=_raw_env(),
        desired_state=_desired_state(),
    )

    assert harness.admin_url == "https://litellm.example.com"
    assert harness.internal_url == "http://wizard-stack-shared-litellm:4000/health/readiness"
    assert [check.name for check in harness.checks] == [
        "public-admin-access",
        "shared-network-readiness",
    ]
    assert "302/401/403" in harness.checks[0].success_criteria
    assert "must never return unauthenticated 200" in harness.checks[0].failure_criteria
    assert "https://litellm.example.com" in harness.checks[0].shell_command
    assert "docker run --rm --network wizard-stack-shared" in harness.checks[1].shell_command
    assert "curlimages/curl:8.7.1" in harness.checks[1].shell_command
    assert "http://wizard-stack-shared-litellm:4000/health/readiness" in harness.checks[1].shell_command


def test_build_litellm_admin_qa_harness_adds_tailnet_probe_when_tailscale_ssh_is_enabled() -> None:
    harness = build_litellm_admin_qa_harness(
        raw_env=_raw_env(),
        desired_state=_desired_state(enable_tailscale=True, tailscale_enable_ssh=True),
    )

    assert [check.name for check in harness.checks] == [
        "public-admin-access",
        "shared-network-readiness",
        "tailnet-host-readiness",
    ]
    tailnet_check = harness.checks[2]
    assert "tailscale ssh wizard-tailnet" in tailnet_check.shell_command
    assert "docker run --rm --network wizard-stack-shared" in tailnet_check.shell_command
    assert "http://wizard-stack-shared-litellm:4000/health/readiness" in tailnet_check.shell_command


@pytest.mark.parametrize("status", (302, 401, 403))
def test_verify_public_litellm_admin_access_accepts_cloudflare_access_challenge(status: int) -> None:
    def _request(_: object) -> _FakeResponse:
        raise error.HTTPError(
            url="https://litellm.example.com",
            code=status,
            msg="challenge",
            hdrs=Message(),
            fp=None,
        )

    verify_public_litellm_admin_access(
        "https://litellm.example.com",
        request_fn=_request,
    )


def test_verify_public_litellm_admin_access_rejects_unauthenticated_200() -> None:
    with pytest.raises(LiteLLMAdminAccessCheckError, match="returned 200"):
        verify_public_litellm_admin_access(
            "https://litellm.example.com",
            request_fn=lambda _: _FakeResponse(status=200),
        )


def _raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "ROOT_DOMAIN": "example.com",
            "STACK_NAME": "wizard-stack",
            "LITELLM_ADMIN_SUBDOMAIN": "litellm",
        },
    )


def _desired_state(*, enable_tailscale: bool = False, tailscale_enable_ssh: bool = False) -> DesiredState:
    return DesiredState(
        format_version=1,
        stack_name="wizard-stack",
        root_domain="example.com",
        dokploy_url="https://dokploy.example.com",
        dokploy_api_url=None,
        enable_tailscale=enable_tailscale,
        tailscale_hostname="wizard-tailnet" if enable_tailscale else None,
        tailscale_enable_ssh=tailscale_enable_ssh,
        tailscale_tags=(),
        tailscale_subnet_routes=(),
        cloudflare_access_otp_emails=("owner@example.com",),
        enabled_features=(),
        selected_packs=(),
        enabled_packs=(),
        hostnames={"dokploy": "dokploy.example.com"},
        seaweedfs_access_key=None,
        seaweedfs_secret_key=None,
        openclaw_gateway_token=None,
        openclaw_channels=(),
        openclaw_replicas=None,
        my_farm_advisor_channels=(),
        my_farm_advisor_replicas=None,
        shared_core=build_shared_core_plan(stack_name="wizard-stack", enabled_packs=()),
    )
