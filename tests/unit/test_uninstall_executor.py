"""Unit tests for uninstall execution side effects on box-local file state."""

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.networking.edge_https import EDGE_HTTPS_FILENAME
from dokploy_wizard.state import OwnershipLedger, RawEnvInput, resolve_desired_state
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
