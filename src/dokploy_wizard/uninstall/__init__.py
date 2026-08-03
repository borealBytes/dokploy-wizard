"""Public uninstall interface."""

from dokploy_wizard.uninstall.confirm import (
    UninstallConfirmationError,
    collect_confirmation_lines,
)
from dokploy_wizard.uninstall.contracts import UninstallBackend
from dokploy_wizard.uninstall.errors import UninstallExecutionError
from dokploy_wizard.uninstall.executor import (
    ShellUninstallBackend,
    execute_uninstall_plan,
)
from dokploy_wizard.uninstall.planner import (
    PlannedDeletion,
    UninstallPlan,
    UninstallPlanningError,
    build_pack_disable_plan,
    build_uninstall_plan,
    compute_remaining_completed_steps,
)
from dokploy_wizard.uninstall.result import UninstallExecutionResult

__all__ = [
    "PlannedDeletion",
    "ShellUninstallBackend",
    "UninstallBackend",
    "UninstallConfirmationError",
    "UninstallExecutionError",
    "UninstallExecutionResult",
    "UninstallPlan",
    "UninstallPlanningError",
    "build_pack_disable_plan",
    "build_uninstall_plan",
    "collect_confirmation_lines",
    "compute_remaining_completed_steps",
    "execute_uninstall_plan",
]
