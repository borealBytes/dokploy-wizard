# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dokploy_wizard.state import OwnershipLedger, RawEnvInput, resolve_desired_state
from dokploy_wizard.state.models import DesiredState
from dokploy_wizard.tailscale import (
    TAILSCALE_NODE_RESOURCE_TYPE,
    CommandResult,
    ShellTailscaleBackend,
    TailscaleError,
    TailscaleManagedResource,
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


def _tailscale_raw_env(**overrides: str) -> RawEnvInput:
    values = {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "ENABLE_TAILSCALE": "true",
        "TAILSCALE_AUTH_KEY": "tskey-auth-123",
        "TAILSCALE_HOSTNAME": "wizard-admin",
        "TAILSCALE_MOCK_INSTALLED": "true",
    }
    values.update(overrides)
    return RawEnvInput(format_version=1, values=values)


def _tailscale_desired_state(**overrides: str) -> DesiredState:
    return resolve_desired_state(_tailscale_raw_env(**overrides))


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
    raw_env = _tailscale_raw_env()
    desired_state = _tailscale_desired_state()
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


def test_shell_tailscale_backend_raises_explicit_error_when_binary_missing() -> None:
    raw_env = _tailscale_raw_env()

    def runner(command: list[str]) -> CommandResult:
        raise FileNotFoundError(command[0])

    backend = ShellTailscaleBackend(raw_env, runner=runner)

    with pytest.raises(
        TailscaleError,
        match=(
            "tailscale command could not be executed because the tailscale "
            "binary was not found on PATH"
        ),
    ):
        backend.get_status("wizard-admin")


def test_shell_tailscale_backend_raises_install_failure_with_stderr() -> None:
    raw_env = _tailscale_raw_env(TAILSCALE_MOCK_INSTALLED="false")

    def runner(command: list[str]) -> CommandResult:
        if command[:2] == ["sh", "-c"]:
            return CommandResult(returncode=1, stdout="", stderr="curl: install failed")
        return CommandResult(returncode=0, stdout="", stderr="")

    backend = ShellTailscaleBackend(raw_env, runner=runner)

    with pytest.raises(
        TailscaleError, match="tailscale install command failed: curl: install failed"
    ):
        backend.apply(
            resource_name="wizard-admin",
            auth_key="tskey-auth-123",
            enable_ssh=False,
            tags=(),
            subnet_routes=(),
        )


def test_shell_tailscale_backend_raises_up_failure_with_stderr() -> None:
    raw_env = _tailscale_raw_env()

    def runner(command: list[str]) -> CommandResult:
        if command[:2] == ["tailscale", "up"]:
            return CommandResult(returncode=1, stdout="", stderr="auth rejected")
        return CommandResult(returncode=0, stdout="", stderr="")

    backend = ShellTailscaleBackend(raw_env, runner=runner)

    with pytest.raises(TailscaleError, match="tailscale up failed: auth rejected"):
        backend.apply(
            resource_name="wizard-admin",
            auth_key="tskey-auth-123",
            enable_ssh=False,
            tags=(),
            subnet_routes=(),
        )


def test_shell_tailscale_backend_rejects_invalid_status_json() -> None:
    raw_env = _tailscale_raw_env()

    def runner(command: list[str]) -> CommandResult:
        if command[:3] == ["tailscale", "status", "--json"]:
            return CommandResult(returncode=0, stdout="not-json", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    backend = ShellTailscaleBackend(raw_env, runner=runner)

    with pytest.raises(TailscaleError, match="tailscale status --json returned invalid JSON"):
        backend.get_status("wizard-admin")


def test_shell_tailscale_backend_requires_self_object() -> None:
    raw_env = _tailscale_raw_env()

    def runner(command: list[str]) -> CommandResult:
        if command[:3] == ["tailscale", "status", "--json"]:
            return CommandResult(returncode=0, stdout='{"BackendState":"Running"}', stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    backend = ShellTailscaleBackend(raw_env, runner=runner)

    with pytest.raises(TailscaleError, match="did not contain a Self object"):
        backend.get_status("wizard-admin")


def test_shell_tailscale_backend_requires_valid_hostname() -> None:
    raw_env = _tailscale_raw_env()

    def runner(command: list[str]) -> CommandResult:
        if command[:3] == ["tailscale", "status", "--json"]:
            return CommandResult(
                returncode=0,
                stdout='{"Self":{"HostName":"","Online":true}}',
                stderr="",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    backend = ShellTailscaleBackend(raw_env, runner=runner)

    with pytest.raises(TailscaleError, match="did not contain a valid hostname"):
        backend.get_status("wizard-admin")


def test_reconcile_tailscale_raises_when_post_apply_verification_fails() -> None:
    raw_env = _tailscale_raw_env()
    desired_state = _tailscale_desired_state()

    @dataclass
    class FakeBackend:
        def get_node(self, resource_id: str) -> TailscaleManagedResource | None:
            del resource_id
            return None

        def find_node_by_name(self, resource_name: str) -> TailscaleManagedResource | None:
            del resource_name
            return None

        def apply(
            self,
            *,
            resource_name: str,
            auth_key: str,
            enable_ssh: bool,
            tags: tuple[str, ...],
            subnet_routes: tuple[str, ...],
        ) -> TailscaleManagedResource:
            del auth_key, enable_ssh, tags, subnet_routes
            return TailscaleManagedResource(
                action="create",
                resource_id=resource_name,
                resource_name=resource_name,
            )

        def get_status(self, resource_name: str) -> None:
            del resource_name
            return None

        def disconnect(self) -> None:
            return None

    backend = FakeBackend()

    with pytest.raises(
        TailscaleError, match="Tailscale apply completed but status verification failed"
    ):
        reconcile_tailscale(
            dry_run=False,
            raw_env=raw_env,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=backend,
        )


def test_build_tailscale_ledger_persists_narrow_scope() -> None:
    updated = build_tailscale_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        node_resource_id="wizard-admin",
    )

    assert updated.resources[0].resource_type == TAILSCALE_NODE_RESOURCE_TYPE
    assert updated.resources[0].scope == "stack:wizard-stack:tailscale"
