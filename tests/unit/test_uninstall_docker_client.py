from __future__ import annotations

import subprocess

import pytest

from dokploy_wizard.uninstall.docker_client import SubprocessDockerDeletionClient
from dokploy_wizard.uninstall.errors import UninstallExecutionError


def test_volume_inspection_rejects_non_absence_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess([], 1, "", "permission denied")

    monkeypatch.setattr(subprocess, "run", denied)

    with pytest.raises(UninstallExecutionError, match="inspection failed"):
        SubprocessDockerDeletionClient().get_volume("volume-1")
