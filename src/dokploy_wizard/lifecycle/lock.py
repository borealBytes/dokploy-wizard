"""Nonblocking lifecycle locks with durable diagnostic metadata."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import sys
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Iterator

from dokploy_wizard.state.models import StateValidationError

_BUSY_EXIT_CODE: Final = 75
_LOCK_ROOT: Final = Path("/run/lock/dokploy-wizard")
_STACK_BINDING_FILE: Final = "lifecycle-stack-binding-v1.json"


class LifecycleLockBusyError(RuntimeError):
    """Raised when another lifecycle operation owns the stack lock."""

    exit_code: Final[int] = _BUSY_EXIT_CODE


class LifecycleStackNameError(ValueError):
    """Raised when a stack name cannot safely identify a lifecycle lock."""


def lifecycle_lock_path(stack_name: str) -> Path:
    """Return the fixed global lifecycle lock path for one validated stack name."""

    if stack_name == "" or "/" in stack_name or stack_name in {".", ".."}:
        raise LifecycleStackNameError("Lifecycle lock stack name is unsafe.")
    return _LOCK_ROOT / f"{stack_name}.lifecycle.lock"


def lifecycle_discovery_lock_path() -> Path:
    """Serialize stack discovery against all lifecycle mutations."""

    return _LOCK_ROOT / ".discovery.lifecycle.lock"


def ensure_lifecycle_stack_binding(state_dir: Path, stack_name: str) -> None:
    """Create or verify the immutable state-directory to stack-lock binding."""

    lifecycle_lock_path(stack_name)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / _STACK_BINDING_FILE
    expected = _binding_bytes(state_dir, stack_name)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        if _read_binding(path) != expected:
            raise StateValidationError(
                "Lifecycle stack binding does not match this state directory."
            )
        return
    try:
        os.write(descriptor, expected)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_lifecycle_stack_binding(state_dir: Path) -> str:
    """Read the immutable stack name without consulting mutable lifecycle state."""

    path = state_dir / _STACK_BINDING_FILE
    encoded = _read_binding(path)
    payload = json.loads(encoded)
    if set(payload) != {"schema_version", "stack", "state_dir_sha256"}:
        raise StateValidationError("Lifecycle stack binding schema is invalid.")
    stack_name = payload["stack"]
    if not isinstance(stack_name, str):
        raise StateValidationError("Lifecycle stack binding stack is invalid.")
    if encoded != _binding_bytes(state_dir, stack_name):
        raise StateValidationError("Lifecycle stack binding identity is invalid.")
    lifecycle_lock_path(stack_name)
    return stack_name


def _binding_bytes(state_dir: Path, stack_name: str) -> bytes:
    payload = {
        "schema_version": 1,
        "stack": stack_name,
        "state_dir_sha256": hashlib.sha256(
            os.fsencode(state_dir.resolve(strict=False))
        ).hexdigest(),
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_binding(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_mode & 0o777 != 0o600 or metadata.st_size > 4096:
            raise StateValidationError("Lifecycle stack binding metadata is invalid.")
        data = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    if len(data) != metadata.st_size:
        raise StateValidationError("Lifecycle stack binding size is invalid.")
    return data


@contextmanager
def lifecycle_operation_lock(
    *,
    state_dir: Path,
    stack_name: str | None,
    command: str,
    shared: bool,
) -> Iterator[str]:
    """Transfer from short discovery serialization to the exact per-stack lock."""

    stack_guard = ExitStack()
    with lifecycle_lock(
        lifecycle_discovery_lock_path(),
        stack_name="discovery",
        command=f"{command}-discovery",
        shared=False,
    ):
        try:
            persisted_stack = load_lifecycle_stack_binding(state_dir)
        except FileNotFoundError:
            persisted_stack = None
        if persisted_stack is not None and stack_name not in {None, persisted_stack}:
            raise StateValidationError(
                "Caller stack conflicts with the immutable lifecycle stack binding."
            )
        if persisted_stack is None and stack_name is None:
            raise StateValidationError(
                "Legacy lifecycle state has no immutable stack binding; provide --stack-name."
            )
        bound_stack = persisted_stack or stack_name
        if bound_stack is None:
            raise StateValidationError("Lifecycle stack identity is unavailable.")
        stack_guard.enter_context(
            lifecycle_lock(
                lifecycle_lock_path(bound_stack),
                stack_name=bound_stack,
                command=command,
                shared=shared,
            )
        )
    try:
        yield bound_stack
    finally:
        stack_guard.close()
def _start_time_ticks() -> int:
    fields = Path("/proc/self/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
    return int(fields[19])


def _write_metadata(descriptor: int, path: Path, *, stack_name: str, command: str) -> None:
    payload = {
        "acquired_at": datetime.now(tz=UTC).isoformat(),
        "argv_sha256": hashlib.sha256(
            b"\0".join(os.fsencode(argument) for argument in sys.argv)
        ).hexdigest(),
        "command": command,
        "pid": os.getpid(),
        "schema_version": 1,
        "stack": stack_name,
        "start_time_ticks": _start_time_ticks(),
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, encoded)
    os.fsync(descriptor)
    os.fchmod(descriptor, 0o600)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def lifecycle_lock(
    path: Path, *, stack_name: str, command: str, shared: bool
) -> Iterator[None]:
    """Hold the lifecycle lock, failing with sysexits EX_TEMPFAIL when busy."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LifecycleLockBusyError(
                f"Lifecycle lock is busy for stack {shlex.quote(stack_name)}."
            ) from error
        if not shared:
            _write_metadata(descriptor, path, stack_name=stack_name, command=command)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
