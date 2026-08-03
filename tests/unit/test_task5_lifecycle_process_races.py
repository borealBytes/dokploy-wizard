from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.lifecycle import lock as lifecycle_lock_module
from dokploy_wizard.lifecycle.lock import LifecycleLockBusyError
from dokploy_wizard.state import RawEnvInput, parse_env_file

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nextcloud.env"


def _modify_worker(raw: RawEnvInput, state_dir: str) -> None:
    try:
        cli.run_modify_flow(
            env_file=_FIXTURE,
            state_dir=Path(state_dir),
            dry_run=False,
            raw_env=raw,
        )
    except LifecycleLockBusyError as error:
        raise SystemExit(error.exit_code) from error


def _uninstall_worker(state_dir: str) -> None:
    try:
        cli.run_uninstall_flow(
            state_dir=Path(state_dir),
            destroy_data=True,
            dry_run=False,
            non_interactive=True,
            confirm_file=None,
        )
    except LifecycleLockBusyError as error:
        raise SystemExit(error.exit_code) from error


@pytest.mark.parametrize("loser", ["modify", "uninstall"])
def test_actual_lifecycle_boundary_rejects_loser_before_state_or_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loser: str,
) -> None:
    context = multiprocessing.get_context("fork")
    raw = parse_env_file(_FIXTURE)
    acquired = context.Event()
    release = context.Event()
    calls = tmp_path / "instrumented-calls.log"
    lock_path = tmp_path / "nextcloud-stack.lifecycle.lock"
    discovery_path = tmp_path / ".discovery.lifecycle.lock"
    lifecycle_lock_module.ensure_lifecycle_stack_binding(
        tmp_path / "state",
        "nextcloud-stack",
    )

    def instrumented_flow(**_: object) -> dict[str, object]:
        calls.write_text("state-read\npreflight\napi-call\n", encoding="utf-8")
        acquired.set()
        if not release.wait(timeout=5):
            raise RuntimeError("winner timed out")
        return {"status": "winner"}

    def forbidden_uninstall(**_: object) -> dict[str, object]:
        with calls.open("a", encoding="utf-8") as stream:
            stream.write("loser-state-read\nloser-api-call\n")
        return {"status": "unexpected"}

    monkeypatch.setattr(cli, "_run_lifecycle_flow", instrumented_flow)
    monkeypatch.setattr(cli, "_run_uninstall_flow_locked", forbidden_uninstall)
    monkeypatch.setattr(lifecycle_lock_module, "lifecycle_lock_path", lambda _: lock_path)
    monkeypatch.setattr(
        lifecycle_lock_module,
        "lifecycle_discovery_lock_path",
        lambda: discovery_path,
    )

    winner = context.Process(target=_modify_worker, args=(raw, str(tmp_path / "state")))
    winner.start()
    assert acquired.wait(timeout=5)
    target = _modify_worker if loser == "modify" else _uninstall_worker
    args = (raw, str(tmp_path / "state")) if loser == "modify" else (str(tmp_path / "state"),)
    losing_process = context.Process(target=target, args=args)
    losing_process.start()
    losing_process.join(timeout=5)
    if losing_process.is_alive():
        losing_process.terminate()
        losing_process.join(timeout=2)
        pytest.fail("losing lifecycle process deadlocked")
    release.set()
    winner.join(timeout=5)
    if winner.is_alive():
        winner.terminate()
        winner.join(timeout=2)
        pytest.fail("winning lifecycle process deadlocked")

    assert winner.exitcode == 0
    assert losing_process.exitcode == 75
    assert calls.read_text(encoding="utf-8") == "state-read\npreflight\napi-call\n"
