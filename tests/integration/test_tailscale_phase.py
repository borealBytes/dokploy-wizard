# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.state import load_state_dir, parse_env_file
from dokploy_wizard.tailscale import CommandResult, ShellTailscaleBackend

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@dataclass
class FakeDokployBackend:
    healthy_before_install: bool = True
    healthy_after_install: bool = True
    install_calls: int = 0

    def is_healthy(self) -> bool:
        return (
            self.healthy_before_install if self.install_calls == 0 else self.healthy_after_install
        )

    def install(self) -> None:
        self.install_calls += 1


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


@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None

    def get_service(self, resource_id: str) -> HeadscaleResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> HeadscaleResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
    ) -> HeadscaleResourceRecord:
        del hostname, secret_refs
        self.existing_service = HeadscaleResourceRecord(
            resource_id="headscale-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service, url
        return True


def test_install_runs_tailscale_phase_after_shared_core(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    raw_env_path = tmp_path / "tailscale.env"
    raw_env_path.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "ENABLE_TAILSCALE=true",
                "TAILSCALE_AUTH_KEY=tskey-auth-123",
                "TAILSCALE_HOSTNAME=wizard-admin",
                "TAILSCALE_ENABLE_SSH=true",
                "HOST_OS_ID=ubuntu",
                "HOST_OS_VERSION_ID=24.04",
                "HOST_CPU_COUNT=2",
                "HOST_MEMORY_GB=4",
                "HOST_DISK_GB=40",
                "HOST_DOCKER_INSTALLED=true",
                "HOST_DOCKER_DAEMON_REACHABLE=true",
                "HOST_PORT_80_IN_USE=false",
                "HOST_PORT_443_IN_USE=false",
                "HOST_PORT_3000_IN_USE=false",
                "HOST_ENVIRONMENT=local",
                "DOKPLOY_BOOTSTRAP_HEALTHY=true",
                "DOKPLOY_BOOTSTRAP_MOCK_API_KEY=dokp-mock-key",
                "CLOUDFLARE_API_TOKEN=token-123",
                "CLOUDFLARE_ACCOUNT_ID=account-123",
                "CLOUDFLARE_ZONE_ID=zone-123",
                "CLOUDFLARE_MOCK_ACCOUNT_OK=true",
                "CLOUDFLARE_MOCK_ZONE_OK=true",
                "TAILSCALE_MOCK_INSTALLED=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runner = FakeRunner()

    summary = run_install_flow(
        env_file=raw_env_path,
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        tailscale_backend=ShellTailscaleBackend(
            raw_env=parse_env_file(raw_env_path), runner=runner
        ),
    )

    loaded = load_state_dir(state_dir)
    assert summary["tailscale"]["outcome"] == "applied"
    phases = summary["lifecycle"]["applicable_phases"]
    assert phases.index("shared_core") < phases.index("tailscale")
    assert loaded.applied_state is not None
    assert "tailscale" in loaded.applied_state.completed_steps
