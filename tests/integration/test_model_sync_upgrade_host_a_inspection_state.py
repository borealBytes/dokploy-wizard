from __future__ import annotations

from pathlib import Path

import pytest

import dokploy_wizard.cli as cli
from dokploy_wizard import proof
from dokploy_wizard.lifecycle import LifecyclePlan, applicable_phases_for
from dokploy_wizard.lifecycle.modify_upgrade import ModifyUpgradeIntent
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    RawEnvInput,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)


def _write_env(path: Path) -> Path:
    path.write_text(
        "\n".join(
            (
                "ROOT_DOMAIN=example.test",
                "STACK_NAME=task18-stack",
                "PACKS=coder",
                "AI_DEFAULT_PROVIDER=openrouter",
                "AI_DEFAULT_MODEL=example/model",
                "AI_DEFAULT_BASE_URL=https://provider.example.test/v1",
                "AI_DEFAULT_API_KEY=unchanged-key",
                "DOKPLOY_ADMIN_EMAIL=operator@example.test",
                "DOKPLOY_ADMIN_PASSWORD=fixture-password",
                "HOST_CPU_COUNT=8",
                "HOST_DISK_GB=200",
                "HOST_DOCKER_DAEMON_REACHABLE=true",
                "HOST_DOCKER_INSTALLED=true",
                "HOST_ENVIRONMENT=local",
                "HOST_MEMORY_GB=16",
                "HOST_OS_ID=ubuntu",
                "HOST_OS_VERSION_ID=24.04",
                "HOST_PORT_3000_IN_USE=false",
                "HOST_PORT_443_IN_USE=false",
                "HOST_PORT_80_IN_USE=false",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_task18_rehydrates_inspection_redactions_before_force_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    state_dir = tmp_path / "state"
    env_file = _write_env(tmp_path / "install.env")
    raw = parse_env_file(env_file)
    desired = resolve_desired_state(raw)
    redacted_values = dict(raw.values)
    redacted_values["AI_DEFAULT_API_KEY"] = "<redacted>"
    redacted_values["DOKPLOY_ADMIN_PASSWORD"] = "<redacted>"
    write_target_state(
        state_dir,
        RawEnvInput(format_version=raw.format_version, values=redacted_values),
        desired,
    )
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=applicable_phases_for(desired),
            runtime_images=desired.runtime_images,
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=desired.format_version, resources=()),
    )
    plans: list[LifecyclePlan] = []

    def capture_plan(**kwargs: LifecyclePlan | proof.JsonValue) -> dict[str, proof.JsonValue]:
        plan = kwargs["lifecycle_plan"]
        assert isinstance(plan, LifecyclePlan)
        plans.append(plan)
        return {"lifecycle": {"mode": plan.mode, "phases_to_run": list(plan.phases_to_run)}}

    monkeypatch.setattr(cli, "active_task1_proof_context", lambda: object())
    monkeypatch.setattr(cli, "preflight_coder_template_migration", lambda *_args: None)
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _raw_env: object())

    def reject_preserved_phase_validation(**_kwargs: proof.JsonValue) -> None:
        pytest.fail("Task 18 explicit upgrade must not expand into preserved phases")

    monkeypatch.setattr(cli, "validate_preserved_phases", reject_preserved_phase_validation)
    monkeypatch.setattr(cli, "execute_lifecycle_plan", capture_plan)

    # When
    summary = cli.run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=True,
        modify_upgrade_intent=ModifyUpgradeIntent.TASK18_HOST_A_MODEL_SYNC,
    )

    # Then
    assert summary["lifecycle"] == {
        "mode": "modify",
        "phases_to_run": ["shared_core", "coder"],
    }
    assert len(plans) == 1
    assert plans[0].raw_equivalent is True
    assert plans[0].desired_equivalent is True


def test_task18_rehydration_preserves_nonsecret_drift() -> None:
    # Given
    existing = RawEnvInput(
        format_version=1,
        values={"DOKPLOY_ADMIN_PASSWORD": "stale-password", "ROOT_DOMAIN": "old.test"},
    )
    requested = RawEnvInput(
        format_version=1,
        values={"DOKPLOY_ADMIN_PASSWORD": "current-password", "ROOT_DOMAIN": "new.test"},
    )

    # When
    rehydrated = cli._rehydrate_inspection_redactions(existing, requested)

    # Then
    assert rehydrated.values == {
        "DOKPLOY_ADMIN_PASSWORD": "current-password",
        "ROOT_DOMAIN": "old.test",
    }
    assert rehydrated != requested
