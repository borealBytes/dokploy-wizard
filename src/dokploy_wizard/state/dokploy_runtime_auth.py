"""State-owned Dokploy API credentials for immutable Task 1 upload environments."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from dokploy_wizard.state.models import DesiredState, RawEnvInput, StateValidationError

_RUNTIME_AUTH_FILE: Final = "dokploy-runtime-auth.json"
_MAX_BYTES: Final = 16 * 1024
_FILE_MODE: Final = 0o600


@dataclass(frozen=True, slots=True)
class DokployRuntimeAuth:
    """Generated Dokploy credentials stored separately from the operator environment."""

    api_url: str
    api_key: str


def load_dokploy_runtime_auth(state_dir: Path) -> DokployRuntimeAuth | None:
    """Load an exact mode-0600 runtime credential file when one was generated."""
    path = state_dir / _RUNTIME_AUTH_FILE
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateValidationError("Dokploy runtime auth state is unreadable") from error
    _require_safe_metadata(metadata)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise StateValidationError("Dokploy runtime auth state is unreadable") from error
    try:
        opened_metadata = os.fstat(descriptor)
        _require_safe_metadata(opened_metadata)
        if (opened_metadata.st_dev, opened_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise StateValidationError("Dokploy runtime auth state is unsafe")
        content = _read_bounded(descriptor)
    except OSError as error:
        raise StateValidationError("Dokploy runtime auth state is unreadable") from error
    finally:
        os.close(descriptor)
    if len(content) > _MAX_BYTES:
        raise StateValidationError("Dokploy runtime auth state is oversized")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateValidationError("Dokploy runtime auth state is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {
        "api_key",
        "api_url",
        "schema_version",
    }:
        raise StateValidationError("Dokploy runtime auth state is invalid")
    api_url = payload["api_url"]
    api_key = payload["api_key"]
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise StateValidationError("Dokploy runtime auth state is invalid")
    if not isinstance(api_url, str) or not isinstance(api_key, str):
        raise StateValidationError("Dokploy runtime auth state is invalid")
    if api_url.strip() == "" or api_key.strip() == "":
        raise StateValidationError("Dokploy runtime auth state is invalid")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if content != canonical:
        raise StateValidationError("Dokploy runtime auth state is not canonical")
    return DokployRuntimeAuth(api_url=api_url, api_key=api_key)


def persist_dokploy_runtime_auth(state_dir: Path, auth: DokployRuntimeAuth) -> None:
    """Atomically persist generated credentials without changing the uploaded environment."""
    if auth.api_url.strip() == "" or auth.api_key.strip() == "":
        raise StateValidationError("Dokploy runtime auth state is incomplete")
    payload = {
        "api_key": auth.api_key,
        "api_url": auth.api_url,
        "schema_version": 1,
    }
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(content) > _MAX_BYTES:
        raise StateValidationError("Dokploy runtime auth state is oversized")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".dokploy-runtime-auth-", dir=state_dir)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, state_dir / _RUNTIME_AUTH_FILE)
        _fsync_directory(state_dir)
    except OSError as error:
        raise StateValidationError("Dokploy runtime auth state cannot be persisted") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            temporary.unlink()


def merge_dokploy_runtime_auth(
    raw_env: RawEnvInput, auth: DokployRuntimeAuth | None
) -> RawEnvInput:
    """Overlay generated credentials while retaining all source-bound environment bytes."""
    if auth is None:
        return raw_env
    values = dict(raw_env.values)
    values["DOKPLOY_API_URL"] = auth.api_url
    values["DOKPLOY_API_KEY"] = auth.api_key
    return RawEnvInput(format_version=raw_env.format_version, values=values)


def merge_dokploy_runtime_auth_desired_state(
    desired_state: DesiredState,
    auth: DokployRuntimeAuth | None,
) -> DesiredState:
    """Project a validated runtime API URL without revalidating merged upload values."""
    if auth is None:
        return desired_state
    return replace(desired_state, dokploy_api_url=auth.api_url)


def _require_safe_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != _FILE_MODE:
        raise StateValidationError("Dokploy runtime auth state is unsafe")


def _read_bounded(descriptor: int) -> bytes:
    remaining = _MAX_BYTES + 1
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if chunk == b"":
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Dokploy runtime auth state short write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
