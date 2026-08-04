"""Bounded canonical-wrapper execution and value-free result parsing."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.litellm.opencode_go_sync_errors import is_sync_failure_category
from dokploy_wizard.proof.model_sync_upgrade_host_a_failures import remote_fixed_failure
from dokploy_wizard.proof.model_sync_upgrade_host_a_observations import (
    HostAObservation,
    parse_host_a_observation,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError

_BLOCKED_CODE = "CODER_RETIRED_WORKSPACE_NOT_STOPPED"
_BLOCKED_PATTERN = re.compile(rb"(?:^|[^A-Z_])CODER_RETIRED_WORKSPACE_NOT_STOPPED(?:$|[^A-Z_])")
_REMOTE_STDOUT_PREFIX = b"[remote:modify:stdout] "
_REMOTE_BEFORE_PREFIX = b"[remote:modify-observation-before:stdout] "
_REMOTE_AFTER_PREFIX = b"[remote:modify-observation-after:stdout] "
_REMOTE_SYNC_ERROR_PATTERN = re.compile(
    rb"^\[remote:modify:stderr\] "
    rb"(?:dokploy_wizard\.state\.sync_schema\.SyncStateError: )?"
    rb"Immediate OpenCode Go sync command failed: "
    rb"([a-z0-9_]+)\.$",
    re.MULTILINE,
)
@dataclass(frozen=True, slots=True)
class ProcessObservation:
    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class ModifyCommandObservation:
    exit_code: int
    failure_code: str | None
    lifecycle_mode: str | None
    phases_to_run: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModifyExecutionObservation:
    command: ModifyCommandObservation
    before: HostAObservation
    after: HostAObservation


def run_bounded_observed_process(
    command: Sequence[str],
    *,
    stdin: bytes,
    output_limit: int,
    timeout_seconds: float,
) -> ProcessObservation:
    """Run a command while retaining both bounded output streams and its exit code."""

    if output_limit < 1 or timeout_seconds <= 0:
        raise UpgradeHostAError("Host A wrapper process limits are invalid")
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise UpgradeHostAError("Host A wrapper process pipes are unavailable")
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    pending = memoryview(stdin)
    deadline = time.monotonic() + timeout_seconds
    failure: str | None = None
    for stream, role in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, role)
    os.set_blocking(process.stdin.fileno(), False)
    if pending:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    try:
        while selector.get_map() and failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timed out"
                break
            for key, _mask in selector.select(remaining):
                if key.data == "stdin":
                    try:
                        pending = pending[os.write(key.fd, pending) :]
                    except BrokenPipeError:
                        pending = pending[len(pending) :]
                    if not pending:
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    stream = process.stdout if key.data == "stdout" else process.stderr
                    stream.close()
                    continue
                target = stdout if key.data == "stdout" else stderr
                target.extend(chunk)
                if len(target) > output_limit:
                    failure = "exceeded output limit"
                    break
        if failure is None:
            exit_code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        else:
            exit_code = -1
    except (OSError, subprocess.TimeoutExpired) as error:
        failure = "failed" if time.monotonic() < deadline else "timed out"
        raise UpgradeHostAError(f"Host A wrapper process {failure}") from error
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    if failure is not None:
        raise UpgradeHostAError(f"Host A wrapper process {failure}")
    return ProcessObservation(exit_code, bytes(stdout), bytes(stderr))


def parse_modify_command(process: ProcessObservation) -> ModifyCommandObservation:
    """Accept only an exact blocker or a successful remote lifecycle summary."""

    summary = _remote_summary(process.stderr)
    if process.exit_code != 0:
        blocker = _BLOCKED_PATTERN.search(process.stderr)
        sync_categories = _REMOTE_SYNC_ERROR_PATTERN.findall(process.stderr)
        if blocker is None and summary is None and len(sync_categories) == 1:
            category = sync_categories[0].decode("ascii")
            if is_sync_failure_category(category):
                raise UpgradeHostAError(f"Host A modify wrapper failed: {category}")
        fixed_failure = remote_fixed_failure(process.stderr)
        if blocker is None and fixed_failure is not None:
            raise UpgradeHostAError(f"Host A modify wrapper failed: {fixed_failure}")
        if blocker is None or summary is not None or sync_categories:
            raise UpgradeHostAError("Host A modify wrapper failed without authoritative blocker")
        return ModifyCommandObservation(process.exit_code, _BLOCKED_CODE, None, ())
    if summary is None:
        raise UpgradeHostAError("Host A modify wrapper summary is absent")
    lifecycle = _mapping(summary.get("lifecycle"), "lifecycle")
    mode = lifecycle.get("mode")
    phases = lifecycle.get("phases_to_run")
    if not isinstance(mode, str) or mode == "" or not isinstance(phases, list):
        raise UpgradeHostAError("Host A modify wrapper lifecycle summary is malformed")
    if not all(isinstance(item, str) and item for item in phases):
        raise UpgradeHostAError("Host A modify wrapper lifecycle phases are malformed")
    return ModifyCommandObservation(process.exit_code, None, mode, tuple(phases))


def parse_modify_execution(
    process: ProcessObservation,
    *,
    remote_root: Path,
) -> ModifyExecutionObservation:
    """Parse lifecycle and both wrapper-captured authoritative observations."""

    return ModifyExecutionObservation(
        parse_modify_command(process),
        parse_host_a_observation(
            _remote_json(process.stderr, _REMOTE_BEFORE_PREFIX, "before observation"),
            remote_root=remote_root,
        ),
        parse_host_a_observation(
            _remote_json(process.stderr, _REMOTE_AFTER_PREFIX, "after observation"),
            remote_root=remote_root,
        ),
    )


def _remote_summary(stderr: bytes) -> dict[str, proof.JsonValue] | None:
    lines = [
        line.removeprefix(_REMOTE_STDOUT_PREFIX)
        for line in stderr.splitlines()
        if line.startswith(_REMOTE_STDOUT_PREFIX)
    ]
    if not lines:
        return None
    try:
        decoded: proof.JsonValue = json.loads(b"\n".join(lines))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpgradeHostAError("Host A modify wrapper summary is malformed") from error
    return _mapping(decoded, "summary")


def _remote_json(
    stderr: bytes,
    prefix: bytes,
    label: str,
) -> dict[str, proof.JsonValue]:
    lines = [line.removeprefix(prefix) for line in stderr.splitlines() if line.startswith(prefix)]
    if not lines:
        raise UpgradeHostAError(f"Host A modify wrapper {label} is absent")
    try:
        decoded: proof.JsonValue = json.loads(b"\n".join(lines))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpgradeHostAError(f"Host A modify wrapper {label} is malformed") from error
    return _mapping(decoded, label)


def _mapping(value: proof.JsonValue | None, label: str) -> dict[str, proof.JsonValue]:
    try:
        return proof.require_mapping(value, label)
    except ValueError as error:
        raise UpgradeHostAError(f"Host A modify wrapper {label} is malformed") from error
