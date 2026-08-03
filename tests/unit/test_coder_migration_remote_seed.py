from __future__ import annotations

import pytest

from tests.integration.coder_migration_remote_docker import RemoteCoderStack, RuntimeImages
from tests.integration.coder_migration_remote_docker_metadata import parse_loopback_port
from tests.integration.coder_migration_remote_seed import _command_cause
from tests.integration.coder_migration_remote_template import (
    WorkspaceTemplateAddress,
    render_template,
    template_push_command,
)


def test_template_renders_attempt_network_once_without_host_gateway() -> None:
    # Given
    template = render_template(WorkspaceTemplateAddress("task4-network", "task4-workspace"))

    # When / Then
    assert template.count("networks_advanced") == 1
    assert 'name = "task4-network"' in template
    assert "host-gateway" not in template
    assert 'resource "docker_image" "workspace"' in template
    assert "image = docker_image.workspace.image_id" in template


def test_template_push_uses_the_pinned_cli_contract() -> None:
    # Given
    container = "coder-test"
    name = "template-test"
    token = "session-token"

    # When
    command = template_push_command(container, name, token)

    # Then
    assert "--create" not in command
    assert "CODER_URL=http://localhost:3000" in command


def test_builtin_provisioners_require_stable_attempt_owned_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    stack = RemoteCoderStack(
        RuntimeImages(coder="coder-image", postgres="postgres-image"), socket_group_id=123
    )
    daemon_counts = iter((3, 3))
    running_checks: list[bool] = []

    def daemon_count(_: str) -> int:
        return next(daemon_counts)

    def provisioner_running() -> bool:
        running_checks.append(True)
        return True

    monkeypatch.setattr(stack, "_provisioner_daemon_count", daemon_count)
    monkeypatch.setattr(stack, "_coder_running", provisioner_running)
    monkeypatch.setattr(
        "tests.integration.coder_migration_remote_docker.time.sleep",
        lambda _: None,
    )

    # When
    stack.wait_for_provisioners("session-token")

    # Then
    assert running_checks == [True, True]


def test_coder_start_uses_docker_assigned_loopback_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    stack = RemoteCoderStack(
        RuntimeImages(coder="coder-image", postgres="postgres-image"), socket_group_id=123
    )
    commands: list[tuple[str, ...]] = []

    def command(arguments: tuple[str, ...]) -> bool:
        commands.append(arguments)
        return True

    monkeypatch.setattr(stack, "_command", command)

    # When
    stack._start_coder()

    # Then
    assert "CODER_PROVISIONER_DAEMONS=3" in commands[0]
    assert "127.0.0.1::3000" in commands[0]
    group_index = commands[0].index("--group-add")
    assert ("--group-add", "123") == commands[0][group_index : group_index + 2]
    assert all("32123" not in argument for argument in commands[0])


def test_remote_coder_port_parser_accepts_docker_assigned_loopback_output() -> None:
    # Given / When
    port = parse_loopback_port(b"127.0.0.1:43210\n")

    # Then
    assert port == 43210


def test_template_push_classifies_invalid_hcl_without_retaining_error_text() -> None:
    # Given / When
    cause = _command_cause(b"Invalid single-argument block definition")

    # Then
    assert cause == "hcl"


def test_template_push_prioritizes_terraform_over_generic_provisioner_text() -> None:
    # Given / When
    cause = _command_cause(b"terraform provisioner failure")

    # Then
    assert cause == "terraform"


def test_template_defines_the_documented_docker_backed_coder_agent_contract() -> None:
    # Given / When
    required_blocks = (
        "docker = {",
        'source = "kreuzwerker/docker"',
        'data "coder_workspace" "me" {}',
        'resource "docker_container" "workspace" {',
        'resource "docker_image" "workspace" {',
        "image = docker_image.workspace.image_id",
        'env = ["CODER_AGENT_TOKEN=${coder_agent.main.token}"]',
        "networks_advanced",
        "coder_agent.main.init_script",
    )

    # Then
    template = render_template(WorkspaceTemplateAddress("task4-network", "task4-workspace"))
    assert all(block in template for block in required_blocks)
