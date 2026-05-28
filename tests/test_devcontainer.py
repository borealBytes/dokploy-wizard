from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_devcontainer_uses_python_311_and_uv_sync_dev_extra() -> None:
    devcontainer_path = REPO_ROOT / ".devcontainer" / "devcontainer.json"
    payload = json.loads(devcontainer_path.read_text(encoding="utf-8"))

    post_create_command = payload["postCreateCommand"]

    assert "3.11" in payload["image"]
    assert "install --upgrade uv" in post_create_command
    assert "uv sync --extra dev" in post_create_command
    assert "uv.lock" not in json.dumps(payload)


def test_pyproject_declares_paramiko_for_devcontainer_sync() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.11"' in pyproject
    assert '"paramiko>=3.0"' in pyproject
