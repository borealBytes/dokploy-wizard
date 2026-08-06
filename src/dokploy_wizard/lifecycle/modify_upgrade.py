from __future__ import annotations

from enum import StrEnum
from typing import Final, assert_never

from dokploy_wizard.lifecycle.changes import LifecyclePlan
from dokploy_wizard.state import AppliedStateCheckpoint, StateValidationError


class ModifyUpgradeIntent(StrEnum):
    OPERATOR = "operator"
    TASK18_HOST_A_MODEL_SYNC = "task18-host-a-model-sync"


_TASK18_PHASES: Final = ("shared_core", "coder")


def apply_modify_upgrade_intent(
    plan: LifecyclePlan,
    intent: ModifyUpgradeIntent,
    applied: AppliedStateCheckpoint,
) -> LifecyclePlan:
    match intent:
        case ModifyUpgradeIntent.OPERATOR:
            return plan
        case ModifyUpgradeIntent.TASK18_HOST_A_MODEL_SYNC:
            if not plan.raw_equivalent:
                raise StateValidationError("Task 18 Host A model-sync upgrade raw input changed.")
            if not plan.desired_equivalent:
                raise StateValidationError(
                    "Task 18 Host A model-sync upgrade desired state changed."
                )
            if plan.mode == "resume":
                legacy_complete = (
                    applied.runtime_images is None
                    and applied.completed_steps == plan.applicable_phases
                )
                same_target_resume = (
                    applied.runtime_images is not None
                    and applied.completed_steps != plan.applicable_phases
                    and plan.preserved_phases == applied.completed_steps
                    and plan.initial_completed_steps == applied.completed_steps
                    and plan.phases_to_run
                    == plan.applicable_phases[len(applied.completed_steps) :]
                )
                if same_target_resume and not all(
                    phase in plan.phases_to_run for phase in _TASK18_PHASES
                ):
                    raise StateValidationError(
                        "Task 18 Host A model-sync resume is missing required phases."
                    )
                if not legacy_complete and not same_target_resume:
                    raise StateValidationError(
                        "Task 18 Host A model-sync lifecycle mode is unsupported."
                    )
            elif plan.mode != "noop":
                raise StateValidationError(
                    "Task 18 Host A model-sync lifecycle mode is unsupported."
                )
            phases_to_run = tuple(
                phase for phase in plan.applicable_phases if phase in _TASK18_PHASES
            )
            if phases_to_run != _TASK18_PHASES:
                raise StateValidationError(
                    "Task 18 Host A model-sync upgrade phases are unavailable."
                )
            preserved_phases = tuple(
                phase for phase in plan.preserved_phases if phase not in _TASK18_PHASES
            )
            start_phase = phases_to_run[0]
            start_index = plan.applicable_phases.index(start_phase)
            return LifecyclePlan(
                mode="modify",
                reasons=("Task 18 Host A model-sync upgrade explicitly requested.",),
                applicable_phases=plan.applicable_phases,
                phases_to_run=phases_to_run,
                preserved_phases=preserved_phases,
                initial_completed_steps=plan.applicable_phases[:start_index],
                start_phase=start_phase,
                raw_equivalent=plan.raw_equivalent,
                desired_equivalent=plan.desired_equivalent,
            )
        case unreachable:
            assert_never(unreachable)
