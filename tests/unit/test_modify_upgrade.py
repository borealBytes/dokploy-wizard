from __future__ import annotations

import pytest

from dokploy_wizard.lifecycle import LifecyclePlan
from dokploy_wizard.lifecycle.modify_upgrade import (
    ModifyUpgradeIntent,
    apply_modify_upgrade_intent,
)
from dokploy_wizard.state import AppliedStateCheckpoint, StateValidationError


def test_task18_accepts_completed_legacy_runtime_binding_upgrade() -> None:
    # Given
    applicable_phases = ("preflight", "shared_core", "coder")
    plan = LifecyclePlan(
        mode="resume",
        reasons=(),
        applicable_phases=applicable_phases,
        phases_to_run=("shared_core", "coder"),
        preserved_phases=("preflight",),
        initial_completed_steps=("preflight",),
        start_phase="shared_core",
        raw_equivalent=True,
        desired_equivalent=True,
    )
    applied = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint="f" * 64,
        completed_steps=applicable_phases,
        runtime_images=None,
    )

    # When
    upgraded = apply_modify_upgrade_intent(
        plan,
        ModifyUpgradeIntent.TASK18_HOST_A_MODEL_SYNC,
        applied,
    )

    # Then
    assert upgraded.mode == "modify"
    assert upgraded.phases_to_run == ("shared_core", "coder")


def test_task18_rejects_incomplete_legacy_runtime_binding_upgrade() -> None:
    # Given
    plan = LifecyclePlan(
        mode="resume",
        reasons=(),
        applicable_phases=("preflight", "shared_core", "coder"),
        phases_to_run=("shared_core", "coder"),
        preserved_phases=("preflight",),
        initial_completed_steps=("preflight",),
        start_phase="shared_core",
        raw_equivalent=True,
        desired_equivalent=True,
    )
    applied = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint="f" * 64,
        completed_steps=("preflight",),
        runtime_images=None,
    )

    # When / Then
    with pytest.raises(StateValidationError, match="checkpoint requires only Task 18 phases"):
        apply_modify_upgrade_intent(
            plan,
            ModifyUpgradeIntent.TASK18_HOST_A_MODEL_SYNC,
            applied,
        )


@pytest.mark.parametrize(
    ("raw_equivalent", "desired_equivalent", "message"),
    (
        (False, True, "raw input changed"),
        (True, False, "desired state changed"),
    ),
)
def test_task18_rejects_changed_target(
    raw_equivalent: bool,
    desired_equivalent: bool,
    message: str,
) -> None:
    # Given
    applicable_phases = ("preflight", "shared_core", "coder")
    plan = LifecyclePlan(
        mode="noop",
        reasons=(),
        applicable_phases=applicable_phases,
        phases_to_run=(),
        preserved_phases=applicable_phases,
        initial_completed_steps=applicable_phases,
        start_phase=None,
        raw_equivalent=raw_equivalent,
        desired_equivalent=desired_equivalent,
    )
    applied = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint="f" * 64,
        completed_steps=applicable_phases,
    )

    # When / Then
    with pytest.raises(StateValidationError, match=message):
        apply_modify_upgrade_intent(
            plan,
            ModifyUpgradeIntent.TASK18_HOST_A_MODEL_SYNC,
            applied,
        )
