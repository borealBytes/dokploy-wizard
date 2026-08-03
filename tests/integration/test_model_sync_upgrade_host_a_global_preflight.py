from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import dokploy_wizard.cli as cli
from dokploy_wizard import proof
from dokploy_wizard.lifecycle import LifecyclePlan, applicable_phases_for
from dokploy_wizard.packs.coder import CoderError, ShellCoderBackend
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)

_BLOCKED_CODE = "CODER_RETIRED_WORKSPACE_NOT_STOPPED"


class _BlockingCoderBackend(ShellCoderBackend):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def preflight_template_migration(self) -> None:
        self._events.append("coder_preflight")
        raise CoderError(f"{_BLOCKED_CODE}: retired workspace is not exactly stopped")


def _write_env(path: Path, api_key: str) -> Path:
    path.write_text(
        "\n".join(
            (
                "ROOT_DOMAIN=example.test",
                "STACK_NAME=task18-stack",
                "PACKS=coder",
                "AI_DEFAULT_PROVIDER=openrouter",
                "AI_DEFAULT_MODEL=example/model",
                "AI_DEFAULT_BASE_URL=https://provider.example.test/v1",
                f"AI_DEFAULT_API_KEY={api_key}",
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


def _seed_state(state_dir: Path, env_file: Path) -> None:
    raw = parse_env_file(env_file)
    desired = resolve_desired_state(raw)
    write_target_state(state_dir, raw, desired)
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


@pytest.mark.parametrize("injected_backend", [False, True])
def test_running_retired_fixture_blocks_before_every_unrelated_mutation_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected_backend: bool,
) -> None:
    # Given
    state_dir = tmp_path / "state"
    old_env = _write_env(tmp_path / "old.env", "old-key")
    requested_env = _write_env(tmp_path / "requested.env", "new-key")
    _seed_state(state_dir, old_env)
    events: list[str] = []

    def unexpected(name: str) -> Callable[..., None]:
        def callback(*_args: proof.JsonValue, **_kwargs: proof.JsonValue) -> None:
            events.append(name)
            raise AssertionError(f"{name} mutation dispatched before Coder preflight")

        return callback

    monkeypatch.setattr(cli, "_docker_login_if_configured", unexpected("docker"))
    monkeypatch.setattr(cli, "ensure_litellm_generated_keys", unexpected("litellm"))
    monkeypatch.setattr(cli, "_ensure_dokploy_api_auth", unexpected("dokploy"))
    monkeypatch.setattr(cli, "write_target_state", unexpected("synchronizer"))
    monkeypatch.setattr(cli, "execute_uninstall_plan", unexpected("control_plane"))

    def provider_mutations(*_args: proof.JsonValue, **_kwargs: proof.JsonValue) -> None:
        events.extend(("shared_core", "litellm_schedule", "cloudflare", "tailscale", "coder"))
        raise AssertionError("provider mutation families dispatched before Coder preflight")

    monkeypatch.setattr(cli, "execute_lifecycle_plan", provider_mutations)
    if injected_backend:
        coder_backend = _BlockingCoderBackend(events)
        coder_migration_preflight = coder_backend.preflight_template_migration
    else:
        coder_backend = None
        coder_migration_preflight = None

        def production_preflight(_hostname: str, _email: str, _password: str) -> None:
            events.append("coder_preflight")
            raise CoderError(f"{_BLOCKED_CODE}: retired workspace is not exactly stopped")

        monkeypatch.setattr(cli, "preflight_coder_template_migration", production_preflight)

    # When / Then
    with pytest.raises(CoderError, match=_BLOCKED_CODE):
        cli.run_modify_flow(
            env_file=requested_env,
            state_dir=state_dir,
            dry_run=False,
            coder_backend=coder_backend,
            coder_migration_preflight=coder_migration_preflight,
        )
    assert events == ["coder_preflight"]


def test_task18_internal_cli_flag_runs_preflight_for_unchanged_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    state_dir = tmp_path / "state"
    env_file = _write_env(tmp_path / "install.env", "unchanged-key")
    env_file.chmod(0o600)
    _seed_state(state_dir, env_file)
    events: list[str] = []

    def block_preflight(_hostname: str, _email: str, _password: str) -> None:
        events.append("coder_preflight")
        raise CoderError(f"{_BLOCKED_CODE}: retired workspace is not exactly stopped")

    def reject_mutation(*_args: proof.JsonValue, **_kwargs: proof.JsonValue) -> None:
        events.append("mutation")
        raise AssertionError("Task 18 mutation dispatched before Coder preflight")

    monkeypatch.setattr(cli, "preflight_coder_template_migration", block_preflight)
    monkeypatch.setattr(cli, "ensure_litellm_generated_keys", reject_mutation)
    monkeypatch.setattr(cli, "execute_lifecycle_plan", reject_mutation)

    # When
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "modify",
                "--env-file",
                str(env_file),
                "--state-dir",
                str(state_dir),
                "--non-interactive",
                "--task18-force-model-sync-upgrade",
            ]
        )

    # Then
    assert isinstance(raised.value.__cause__, CoderError)
    assert _BLOCKED_CODE in str(raised.value.__cause__)
    assert events == ["coder_preflight"]


@pytest.mark.parametrize(
    ("extra_arguments", "expected_mode", "expected_phases"),
    [
        ((), "noop", ()),
        (("--task18-force-model-sync-upgrade",), "modify", ("shared_core", "coder")),
    ],
)
def test_task18_internal_cli_flag_alone_forces_model_sync_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_arguments: tuple[str, ...],
    expected_mode: str,
    expected_phases: tuple[str, ...],
) -> None:
    # Given
    state_dir = tmp_path / "state"
    env_file = _write_env(tmp_path / "install.env", "unchanged-key")
    env_file.chmod(0o600)
    _seed_state(state_dir, env_file)
    plans: list[LifecyclePlan] = []

    def capture_plan(
        **kwargs: LifecyclePlan | proof.JsonValue,
    ) -> dict[str, proof.JsonValue]:
        plan = kwargs["lifecycle_plan"]
        assert isinstance(plan, LifecyclePlan)
        plans.append(plan)
        return {"lifecycle": {"mode": plan.mode, "phases_to_run": list(plan.phases_to_run)}}

    monkeypatch.setattr(cli, "preflight_coder_template_migration", lambda *_args: None)
    monkeypatch.setattr(cli, "CloudflareApiBackend", lambda _raw_env: object())
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "execute_lifecycle_plan", capture_plan)

    # When
    exit_code = cli.main(
        [
            "modify",
            "--env-file",
            str(env_file),
            "--state-dir",
            str(state_dir),
            "--dry-run",
            "--non-interactive",
            *extra_arguments,
        ]
    )

    # Then
    assert exit_code == 0
    assert len(plans) == 1
    assert plans[0].mode == expected_mode
    assert plans[0].phases_to_run == expected_phases
