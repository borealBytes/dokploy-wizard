# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dokploy_wizard.state import OwnershipLedger, RawEnvInput, resolve_desired_state
from dokploy_wizard.tailscale import (
    TAILSCALE_NODE_RESOURCE_TYPE,
    CommandResult,
    ShellTailscaleBackend,
    build_tailscale_ledger,
    reconcile_tailscale,
)


@dataclass
class FakeRunner:
    commands: list[list[str]] = field(default_factory=list)
    up_applied: bool = False

    def __call__(self, command: list[str]) -> CommandResult:
        self.commands.append(command)
        if command[:2] == ["tailscale", "up"]:
            self.up_applied = True
            return CommandResult(returncode=0, stdout="", stderr="")
        if command[:3] == ["tailscale", "status", "--json"]:
            if not self.up_applied:
                return CommandResult(returncode=1, stdout="", stderr="not connected")
            return CommandResult(
                returncode=0,
                stdout='{"Self":{"HostName":"wizard-admin","Online":true,"LoginName":"user@example.com"}}',
                stderr="",
            )
        if command[:3] == ["tailscale", "ip", "-4"]:
            return CommandResult(returncode=0, stdout="100.64.0.10\n", stderr="")
        if command[:3] == ["tailscale", "ip", "-6"]:
            return CommandResult(returncode=0, stdout="fd7a:115c:a1e0::10\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")


def test_resolve_desired_state_supports_tailscale_config() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_TAILSCALE": "true",
                "TAILSCALE_AUTH_KEY": "tskey-auth-123",
                "TAILSCALE_HOSTNAME": "wizard-admin",
                "TAILSCALE_ENABLE_SSH": "true",
                "TAILSCALE_TAGS": "tag:admin,tag:ops",
                "TAILSCALE_SUBNET_ROUTES": "10.0.0.0/24,10.1.0.0/24",
            },
        )
    )

    assert desired_state.enable_tailscale is True
    assert desired_state.tailscale_hostname == "wizard-admin"
    assert desired_state.tailscale_enable_ssh is True
    assert desired_state.tailscale_tags == ("tag:admin", "tag:ops")
    assert desired_state.tailscale_subnet_routes == ("10.0.0.0/24", "10.1.0.0/24")


def test_resolve_desired_state_rejects_tailscale_config_when_disabled() -> None:
    with pytest.raises(ValueError, match="ENABLE_TAILSCALE=true"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "TAILSCALE_HOSTNAME": "wizard-admin",
                },
            )
        )


def test_reconcile_tailscale_skips_when_disabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
            },
        )
    )

    phase = reconcile_tailscale(
        dry_run=True,
        raw_env=RawEnvInput(
            format_version=1, values={"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"}
        ),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=ShellTailscaleBackend(
            RawEnvInput(
                format_version=1,
                values={"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"},
            )
        ),
    )

    assert phase.result.outcome == "skipped"


def test_shell_tailscale_backend_applies_and_verifies_with_fake_runner() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_TAILSCALE": "true",
            "TAILSCALE_AUTH_KEY": "tskey-auth-123",
            "TAILSCALE_HOSTNAME": "wizard-admin",
            "TAILSCALE_MOCK_INSTALLED": "true",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    runner = FakeRunner()
    backend = ShellTailscaleBackend(raw_env, runner=runner)

    phase = reconcile_tailscale(
        dry_run=False,
        raw_env=raw_env,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.status is not None
    assert phase.result.status.hostname == "wizard-admin"
    assert phase.result.status.ipv4 == "100.64.0.10"
    assert any(command[:2] == ["tailscale", "up"] for command in runner.commands)


def test_build_tailscale_ledger_persists_narrow_scope() -> None:
    updated = build_tailscale_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        node_resource_id="wizard-admin",
    )

    assert updated.resources[0].resource_type == TAILSCALE_NODE_RESOURCE_TYPE
    assert updated.resources[0].scope == "stack:wizard-stack:tailscale"
