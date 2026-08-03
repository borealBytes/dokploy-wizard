from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeDetail:
    exists: bool
    running: bool
    attached: bool
    dns_reachable: bool
    health_reachable: bool
    agent_present: bool


def runtime_category(detail: WorkspaceRuntimeDetail) -> str:
    if not detail.exists:
        return "workspace-container-missing"
    if not detail.running:
        return "workspace-container-exited"
    if not detail.attached:
        return "workspace-network-detached"
    if not detail.dns_reachable:
        return "workspace-dns-unreachable"
    if not detail.health_reachable:
        return "workspace-coder-http-unreachable"
    if not detail.agent_present:
        return "workspace-agent-process-absent"
    return "workspace-agent-registration"


class WorkspaceRuntimeProbe:
    def __init__(self, workspace: str, network: str, coder: str) -> None:
        self._workspace = workspace
        self._network = network
        self._coder = coder

    def __call__(self) -> str:
        exists = self._returncode(("docker", "inspect", self._workspace)) == 0
        if not exists:
            return runtime_category(
                WorkspaceRuntimeDetail(False, False, False, False, False, False)
            )
        running = self._fixed_true(
            ("docker", "inspect", "--format", "{{.State.Running}}", self._workspace)
        )
        if not running:
            return runtime_category(WorkspaceRuntimeDetail(True, False, False, False, False, False))
        attached = self._fixed_true(
            (
                "docker",
                "inspect",
                "--format",
                f"{{{{if index .NetworkSettings.Networks \"{self._network}\"}}}}true{{{{end}}}}",
                self._workspace,
            )
        )
        if not attached:
            return runtime_category(WorkspaceRuntimeDetail(True, True, False, False, False, False))
        dns_reachable = self._returncode(
            ("docker", "exec", self._workspace, "getent", "hosts", self._coder)
        ) == 0
        health_reachable = self._returncode(
            ("docker", "exec", self._workspace, "wget", "-q", "-O", "/dev/null", f"http://{self._coder}:3000/healthz")
        ) == 0
        agent_present = self._returncode(
            ("docker", "exec", self._workspace, "pgrep", "-f", "coder.*agent")
        ) == 0
        detail = WorkspaceRuntimeDetail(
            True, True, True, dns_reachable, health_reachable, agent_present
        )
        return runtime_category(detail)

    def _returncode(self, command: tuple[str, ...]) -> int:
        return subprocess.run(command, check=False, capture_output=True).returncode

    def _fixed_true(self, command: tuple[str, ...]) -> bool:
        result = subprocess.run(command, check=False, capture_output=True)
        return result.returncode == 0 and result.stdout == b"true\n"
