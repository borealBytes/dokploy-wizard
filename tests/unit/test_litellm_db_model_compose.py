from __future__ import annotations

from dokploy_wizard.core import build_shared_core_plan
from dokploy_wizard.dokploy.shared_core import _render_compose_file


def test_litellm_compose_enables_database_model_management() -> None:
    # Given
    plan = build_shared_core_plan(stack_name="wizard-stack", enabled_packs=())

    # When
    compose = _render_compose_file(plan, {}).compose_file

    # Then
    assert 'STORE_MODEL_IN_DB: "True"' in compose
