"""Exact immediate OpenCode Go sync command execution boundary."""

from __future__ import annotations

import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from dokploy_wizard.state.sync_schema import JsonValue, SyncStateError, canonical_digest

_SYNC_ERROR_PATTERN = re.compile(r"^DOKPLOY_WIZARD_SYNC_ERROR=([a-z_]+)$", re.MULTILINE)
_SYNC_ERROR_CATEGORIES = frozenset(
    {
        "catalog_source",
        "catalog_state",
        "model_admin",
        "persistence",
        "runtime_config",
        "runtime_lock",
        "runtime_unknown",
    }
)


@dataclass(frozen=True, slots=True)
class ImmediateSyncCommand:
    service_name: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    state_root: Path


@dataclass(frozen=True, slots=True)
class ImmediateSyncExecution:
    parent_exit_code: int
    before_snapshot_sha256: str
    after_snapshot_sha256: str
    parent_reconcile_sha256: str
    durable_write_delta: tuple[str, ...]


class ImmediateSyncExecutor(Protocol):
    def execute(self, command: ImmediateSyncCommand) -> ImmediateSyncExecution: ...


class ProcessRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class DockerExecImmediateSyncExecutor:
    """Run the exact sync argv and env in the deployed LiteLLM service container."""

    runner: ProcessRunner | None = None

    def execute(self, command: ImmediateSyncCommand) -> ImmediateSyncExecution:
        before = _snapshot(command.state_root)
        arguments = tuple(
            item
            for name, value in command.env
            for item in ("--env", f"{name}={value}")
        )
        runner = self.runner or _run_process
        result = runner(
            ("docker", "exec", *arguments, command.service_name, *command.argv),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            categories = tuple(_SYNC_ERROR_PATTERN.findall(result.stderr))
            if len(categories) == 1 and categories[0] in _SYNC_ERROR_CATEGORIES:
                raise SyncStateError(
                    f"Immediate OpenCode Go sync command failed: {categories[0]}."
                )
            raise SyncStateError("Immediate OpenCode Go sync command failed.")
        after = _snapshot(command.state_root)
        delta = tuple(
            sorted(
                path
                for path in set(before) | set(after)
                if before.get(path) != after.get(path)
            )
        )
        return ImmediateSyncExecution(
            parent_exit_code=result.returncode,
            before_snapshot_sha256=canonical_digest(before),
            after_snapshot_sha256=canonical_digest(after),
            parent_reconcile_sha256=canonical_digest(
                {
                    "argv": list(command.argv),
                    "durable_write_delta": list(delta),
                    "env": [list(item) for item in command.env],
                    "exit_code": result.returncode,
                }
            ),
            durable_write_delta=delta,
        )


def parse_immediate_sync_command(
    command: str,
    *,
    owner_id: str,
    service_name: str,
    state_root: Path,
) -> ImmediateSyncCommand:
    """Parse and bind the byte-identical owner-marked schedule command."""

    parts = tuple(shlex.split(command, posix=True))
    expected_env = (
        ("DOKPLOY_WIZARD_SCHEDULE_OWNER_ID", owner_id),
        ("TZ", "UTC"),
    )
    expected_prefix = tuple(f"{name}={value}" for name, value in expected_env)
    if parts[:2] != expected_prefix or len(parts) < 3:
        raise SyncStateError("Immediate sync command owner environment is invalid.")
    argv = parts[2:]
    if command != " ".join((*expected_prefix, *argv)):
        raise SyncStateError("Immediate sync command is not canonical.")
    return ImmediateSyncCommand(
        service_name=service_name,
        argv=argv,
        env=expected_env,
        state_root=state_root,
    )


def _run_process(
    arguments: tuple[str, ...],
    *,
    check: bool,
    capture_output: bool,
    text: bool,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=check,
        capture_output=capture_output,
        text=text,
    )


def _snapshot(root: Path) -> dict[str, JsonValue]:
    snapshot: dict[str, JsonValue] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            relative.parts[0].startswith("lease-")
            or relative.parts[0] in {"create-intents", "runtime", "sync-helper-env"}
            or relative.as_posix() == "sync.lock"
        ):
            continue
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SyncStateError("Immediate sync state contains a non-regular path.")
        snapshot[relative.as_posix()] = sha256(path.read_bytes()).hexdigest()
    return snapshot
