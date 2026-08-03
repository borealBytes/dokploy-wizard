from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.lifecycle import lock as lifecycle_lock_module
from dokploy_wizard.lifecycle.lock import (
    ensure_lifecycle_stack_binding,
    lifecycle_operation_lock,
)
from dokploy_wizard.state import StateValidationError


def _redirect_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        lifecycle_lock_module,
        "lifecycle_lock_path",
        lambda stack: tmp_path / "locks" / f"{stack}.lock",
    )
    monkeypatch.setattr(
        lifecycle_lock_module,
        "lifecycle_discovery_lock_path",
        lambda: tmp_path / "locks" / "discovery.lock",
    )


def test_operation_lock_rejects_explicit_stack_conflicting_with_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    _redirect_locks(monkeypatch, tmp_path)
    ensure_lifecycle_stack_binding(state_dir, "bound-stack")

    with pytest.raises(StateValidationError, match="conflicts"):
        with lifecycle_operation_lock(
            state_dir=state_dir,
            stack_name="caller-stack",
            command="modify",
            shared=False,
        ):
            pytest.fail("conflicting stack reached the lifecycle body")

    assert not (tmp_path / "locks" / "caller-stack.lock").exists()


def test_uninstall_requires_explicit_stack_for_unbound_legacy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "desired-state.json").write_text(
        json.dumps({"stack_name": "legacy-stack"}),
        encoding="utf-8",
    )
    _redirect_locks(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli,
        "_run_uninstall_flow_locked",
        lambda **_: pytest.fail("uninstall read mutable state without an explicit stack"),
    )

    with pytest.raises(StateValidationError, match="provide --stack-name"):
        cli.run_uninstall_flow(
            state_dir=state_dir,
            destroy_data=False,
            dry_run=True,
            non_interactive=True,
            confirm_file=None,
            stack_name=None,
        )

    assert not (tmp_path / "locks" / "legacy-stack.lock").exists()
