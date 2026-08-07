from __future__ import annotations

from pathlib import Path

import pytest

import dokploy_wizard.cli as cli
from dokploy_wizard import proof
from dokploy_wizard.lifecycle import (
    LifecyclePlan,
    applicable_phases_for,
    classify_modify_request,
)
from dokploy_wizard.lifecycle.modify_upgrade import ModifyUpgradeIntent
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    OwnershipLedger,
    RawEnvInput,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.dokploy_runtime_auth import DokployRuntimeAuth


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


@pytest.mark.parametrize("task1_context", (object(), None))
def test_task18_rehydrates_inspection_redactions_before_force_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task1_context: object | None,
) -> None:
    # Given
    state_dir = tmp_path / "state"
    env_file = _write_env(tmp_path / "install.env")
    raw = parse_env_file(env_file)
    desired = resolve_desired_state(raw)
    redacted_values = dict(raw.values)
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

    monkeypatch.setattr(cli, "active_task1_proof_context", lambda: task1_context)
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


@pytest.mark.parametrize("requested_password", ("fixture-password", None))
def test_operator_modify_still_rejects_inactive_admin_only_change(
    tmp_path: Path,
    requested_password: str | None,
) -> None:
    # Given
    env_file = _write_env(tmp_path / "operator-modify.env")
    requested_from_file = parse_env_file(env_file)
    requested_values = dict(requested_from_file.values)
    if requested_password is None:
        requested_values.pop("DOKPLOY_ADMIN_PASSWORD")
    else:
        requested_values["DOKPLOY_ADMIN_PASSWORD"] = requested_password
    requested = RawEnvInput(
        format_version=requested_from_file.format_version,
        values=requested_values,
    )
    desired = resolve_desired_state(requested)
    existing = RawEnvInput(
        format_version=requested.format_version,
        values={**requested.values, "DOKPLOY_ADMIN_PASSWORD": "previous-password"},
    )
    applied = AppliedStateCheckpoint(
        format_version=desired.format_version,
        desired_state_fingerprint=desired.fingerprint(),
        completed_steps=applicable_phases_for(desired),
        runtime_images=desired.runtime_images,
    )

    # When / Then
    with pytest.raises(ValueError, match="inactive_dokploy_admin"):
        classify_modify_request(
            existing_raw=existing,
            existing_desired=desired,
            existing_applied=applied,
            existing_ledger=OwnershipLedger(format_version=desired.format_version, resources=()),
            requested_raw=requested,
            requested_desired=desired,
        )


def test_task18_inspection_snapshot_uses_normalized_raw_equivalence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    state_dir = tmp_path / "state"
    env_file = _write_env(tmp_path / "install.env")
    env_raw = parse_env_file(env_file)
    raw = RawEnvInput(
        format_version=env_raw.format_version,
        values={
            **env_raw.values,
            "PACKS": "coder,seaweedfs",
            "SEAWEEDFS_ACCESS_KEY": "fixture-access-key",
            "SEAWEEDFS_SECRET_KEY": "fixture-secret-key",
        },
    )
    desired = resolve_desired_state(raw)
    redacted_desired_payload = desired.to_dict()
    redacted_desired_payload["seaweedfs_access_key"] = "<redacted>"
    redacted_desired_payload["seaweedfs_secret_key"] = "<redacted>"
    redacted_desired = DesiredState.from_dict(redacted_desired_payload)
    write_target_state(
        state_dir,
        cli._redacted_raw_env_input(raw),
        redacted_desired,
    )
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=redacted_desired.format_version,
            desired_state_fingerprint=redacted_desired.fingerprint(),
            completed_steps=applicable_phases_for(redacted_desired),
            runtime_images=redacted_desired.runtime_images,
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(format_version=desired.format_version, resources=()),
    )
    monkeypatch.setattr(cli, "active_task1_proof_context", lambda: object())
    monkeypatch.setattr(
        cli,
        "load_dokploy_runtime_auth",
        lambda _state_dir: DokployRuntimeAuth(
            api_url="https://runtime.example.test/api",
            api_key="runtime-fixture-key",
        ),
    )
    monkeypatch.setattr(cli, "preflight_coder_template_migration", lambda *_args: None)
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _raw_env: object())
    monkeypatch.setattr(cli, "_build_coder_backend", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **kwargs: {
            "lifecycle": {
                "mode": kwargs["lifecycle_plan"].mode,
                "phases_to_run": list(kwargs["lifecycle_plan"].phases_to_run),
            }
        },
    )

    # When
    summary = cli.run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=True,
        raw_env=raw,
        modify_upgrade_intent=ModifyUpgradeIntent.TASK18_HOST_A_MODEL_SYNC,
    )

    # Then
    assert summary["lifecycle"] == {
        "mode": "modify",
        "phases_to_run": ["shared_core", "coder"],
    }
