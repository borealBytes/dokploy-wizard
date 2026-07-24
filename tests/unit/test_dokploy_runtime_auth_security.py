from __future__ import annotations

import stat
from pathlib import Path

import pytest

from dokploy_wizard.state import StateValidationError
from dokploy_wizard.state.dokploy_runtime_auth import (
    DokployRuntimeAuth,
    load_dokploy_runtime_auth,
    persist_dokploy_runtime_auth,
)

_AUTH_FILE = "dokploy-runtime-auth.json"
_SECRET = "runtime-secret-value"
_CANONICAL = (
    b'{"api_key":"runtime-secret-value","api_url":"http://127.0.0.1:3000","schema_version":1}\n'
)


def test_persist_runtime_auth_preserves_state_directory_mode(tmp_path: Path) -> None:
    # Given
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o750)

    # When
    persist_dokploy_runtime_auth(
        state_dir,
        DokployRuntimeAuth(
            api_url="http://127.0.0.1:3000",
            api_key=_SECRET,
        ),
    )

    # Then
    auth_path = state_dir / _AUTH_FILE
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o750
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    assert auth_path.read_bytes() == _CANONICAL


@pytest.mark.parametrize(
    ("content", "mode"),
    [
        pytest.param(_CANONICAL, 0o640, id="wrong-mode"),
        pytest.param(
            b'{"api_key": "runtime-secret-value", "api_url": '
            b'"http://127.0.0.1:3000", "schema_version": 1}\n',
            0o600,
            id="noncanonical",
        ),
        pytest.param(
            b"runtime-secret-value is not json\n",
            0o600,
            id="corrupt-json",
        ),
        pytest.param(
            b'{"api_key":"","api_url":"http://127.0.0.1:3000","schema_version":1}\n',
            0o600,
            id="empty-api-key",
        ),
        pytest.param(
            b'{"api_key":"runtime-secret-value","api_url":"","schema_version":1}\n',
            0o600,
            id="empty-api-url",
        ),
        pytest.param(
            b'{"api_key":"runtime-secret-value","api_url":"   ","schema_version":1}\n',
            0o600,
            id="blank-api-url",
        ),
        pytest.param(
            b'{"api_key":"runtime-secret-value","api_url":'
            b'"http://127.0.0.1:3000","extra":"forbidden","schema_version":1}\n',
            0o600,
            id="unexpected-key",
        ),
    ],
)
def test_load_runtime_auth_rejects_unsafe_content_without_leaking_secrets(
    tmp_path: Path,
    content: bytes,
    mode: int,
) -> None:
    # Given
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    auth_path = state_dir / _AUTH_FILE
    auth_path.write_bytes(content)
    auth_path.chmod(mode)

    # When
    with pytest.raises(StateValidationError) as raised:
        load_dokploy_runtime_auth(state_dir)

    # Then
    assert _SECRET not in str(raised.value)


def test_load_runtime_auth_rejects_symlink_without_leaking_secrets(tmp_path: Path) -> None:
    # Given
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "secret-target.json"
    target.write_bytes(_CANONICAL)
    target.chmod(0o600)
    (state_dir / _AUTH_FILE).symlink_to(target)

    # When
    with pytest.raises(StateValidationError) as raised:
        load_dokploy_runtime_auth(state_dir)

    # Then
    assert _SECRET not in str(raised.value)


def test_load_runtime_auth_rejects_oversized_file_without_leaking_secrets(
    tmp_path: Path,
) -> None:
    # Given
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    auth_path = state_dir / _AUTH_FILE
    auth_path.write_bytes(_SECRET.encode() + b"x" * (16 * 1024))
    auth_path.chmod(0o600)

    # When
    with pytest.raises(StateValidationError) as raised:
        load_dokploy_runtime_auth(state_dir)

    # Then
    assert _SECRET not in str(raised.value)


def test_persist_runtime_auth_rejects_oversized_credentials(tmp_path: Path) -> None:
    # Given
    state_dir = tmp_path / "state"
    oversized_key = "x" * (16 * 1024)

    # When
    with pytest.raises(StateValidationError) as raised:
        persist_dokploy_runtime_auth(
            state_dir,
            DokployRuntimeAuth(
                api_url="http://127.0.0.1:3000",
                api_key=oversized_key,
            ),
        )

    # Then
    assert oversized_key not in str(raised.value)
    assert not (state_dir / _AUTH_FILE).exists()
