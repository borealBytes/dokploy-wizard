"""Unit tests for uninstall execution side effects on box-local file state."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from dokploy_wizard.networking.edge_https import EDGE_HTTPS_FILENAME
from dokploy_wizard.state import OwnershipLedger, RawEnvInput, resolve_desired_state
from dokploy_wizard.uninstall import executor
from dokploy_wizard.uninstall.executor import execute_uninstall_plan
from dokploy_wizard.uninstall.planner import PlannedDeletion, UninstallPlan


class RecordingBackend:
    def __init__(self) -> None:
        self.deleted: list[PlannedDeletion] = []

    def delete(self, deletion: PlannedDeletion) -> None:
        self.deleted.append(deletion)


def test_uninstall_removes_edge_https_dropin(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    traefik_dir = tmp_path / "traefik-dynamic"
    traefik_dir.mkdir()
    dropin = traefik_dir / EDGE_HTTPS_FILENAME
    dropin.write_text("http:\n  routers: {}\n", encoding="utf-8")

    desired_state = resolve_desired_state(
        RawEnvInput(format_version=1, values={"STACK_NAME": "s", "ROOT_DOMAIN": "example.com"})
    )
    plan = UninstallPlan(
        mode="destroy-data",
        environment="local",
        deletions=(),
        retained_resources=(),
        warnings=(),
    )

    result = execute_uninstall_plan(
        state_dir=state_dir,
        raw_input=RawEnvInput(format_version=1, values={"STACK_NAME": "s"}),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        plan=plan,
        backend=RecordingBackend(),
        dry_run=False,
        traefik_dynamic_dir=traefik_dir,
    )

    assert not dropin.exists()
    assert result.state_cleared


def test_uninstall_survives_missing_edge_dropin(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    desired_state = resolve_desired_state(
        RawEnvInput(format_version=1, values={"STACK_NAME": "s", "ROOT_DOMAIN": "example.com"})
    )
    plan = UninstallPlan(
        mode="retain-data",
        environment="local",
        deletions=(),
        retained_resources=(),
        warnings=(),
    )

    result = execute_uninstall_plan(
        state_dir=state_dir,
        raw_input=RawEnvInput(format_version=1, values={"STACK_NAME": "s"}),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        plan=plan,
        backend=RecordingBackend(),
        dry_run=False,
        traefik_dynamic_dir=tmp_path / "no-traefik",
    )

    # Dokploy teardown may remove the whole directory before we get there.
    assert not (tmp_path / "no-traefik").exists()
    assert result.state_cleared


class _FakeDocker:
    """Scripted docker CLI seam: ls payloads in, rm verdicts out."""

    def __init__(
        self,
        networks: str = "",
        volumes: str = "",
        fails: tuple[str, ...] = (),
    ) -> None:
        self.networks = networks
        self.volumes = volumes
        self.fails = fails
        self.commands: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(args))
        proc = subprocess.CompletedProcess(["docker", *args], 0, "", "")
        if args[:2] == ["network", "ls"]:
            proc.stdout = self.networks
        elif args[:2] == ["volume", "ls"]:
            proc.stdout = self.volumes
        elif args[:2] in (["network", "rm"], ["volume", "rm"]) and args[2] in self.fails:
            proc.returncode = 1
        return proc


@pytest.fixture(autouse=True)
def _no_real_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reclaim path must never touch a host daemon in unit tests; the
    # reclaim tests script their own fake through the same seam.
    monkeypatch.setattr(executor, "_docker", _FakeDocker())


def _run_uninstall(tmp_path: Path, mode: str) -> Any:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={"STACK_NAME": "showmeseewhy-com", "ROOT_DOMAIN": "example.com"},
        )
    )
    plan = UninstallPlan(
        mode=mode,
        environment="showmeseewhy-com",
        deletions=(),
        retained_resources=(),
        warnings=(),
    )
    return execute_uninstall_plan(
        state_dir=state_dir,
        raw_input=RawEnvInput(
            format_version=1, values={"STACK_NAME": "showmeseewhy-com"}
        ),
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        plan=plan,
        backend=RecordingBackend(),
        dry_run=False,
        traefik_dynamic_dir=tmp_path / "no-traefik",
    )


def test_destroy_uninstall_reclaims_project_networks_and_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker(
        networks=(
            "showmeseewhy-com-gitea-dli8ob_default\n"
            "showmeseewhy-com-shared\n"
            "showmeseewhy-com-filex-ontm76_default\n"
        ),
        volumes="showmeseewhy-com-gitea-dli8ob_showmeseewhy-com-gitea-data\n",
        fails=("showmeseewhy-com-shared",),
    )
    monkeypatch.setattr(executor, "_docker", fake)

    result = _run_uninstall(tmp_path, mode="destroy")

    assert result.reclaimed_docker_state == {
        "networks": [
            "showmeseewhy-com-gitea-dli8ob_default",
            "showmeseewhy-com-filex-ontm76_default",
        ],
        "volumes": ["showmeseewhy-com-gitea-dli8ob_showmeseewhy-com-gitea-data"],
        "skipped": ["showmeseewhy-com-shared"],
    }
    assert ["network", "ls", "-q", "--filter", "name=^showmeseewhy-com-"] in fake.commands


def test_retain_uninstall_reclaims_networks_but_spares_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker(
        networks="showmeseewhy-com-gitea-dli8ob_default\n",
        volumes="showmeseewhy-com-gitea-dli8ob_showmeseewhy-com-gitea-data\n",
    )
    monkeypatch.setattr(executor, "_docker", fake)

    result = _run_uninstall(tmp_path, mode="retain")

    assert result.reclaimed_docker_state == {
        "networks": ["showmeseewhy-com-gitea-dli8ob_default"],
        "volumes": [],
        "skipped": [],
    }
    assert not [c for c in fake.commands if c[:2] == ["volume", "rm"]]


def test_reclaim_survives_a_box_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(executor, "_docker", missing)

    result = _run_uninstall(tmp_path, mode="destroy")

    assert result.reclaimed_docker_state == {"networks": [], "volumes": [], "skipped": []}
