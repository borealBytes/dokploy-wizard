"""Typed uninstall execution result."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.uninstall.planner import PlannedDeletion


@dataclass(frozen=True)
class UninstallExecutionResult:
    deleted_resources: tuple[PlannedDeletion, ...]
    remaining_completed_steps: tuple[str, ...]
    state_cleared: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_resources": [item.to_dict() for item in self.deleted_resources],
            "remaining_completed_steps": list(self.remaining_completed_steps),
            "state_cleared": self.state_cleared,
        }


def cap_completed_steps(
    completed_steps: tuple[str, ...], ceiling: tuple[str, ...] | None
) -> tuple[str, ...]:
    if ceiling is None or len(completed_steps) <= len(ceiling):
        return completed_steps
    return ceiling
