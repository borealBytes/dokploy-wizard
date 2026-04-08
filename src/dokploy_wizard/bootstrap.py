"""Dokploy bootstrap planning and reconciliation."""

from __future__ import annotations

import http.client
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.state import RawEnvInput, StateValidationError

DOKPLOY_INSTALL_COMMAND = "curl -sSL https://dokploy.com/install.sh | sh"
LOCAL_HEALTH_URL = "http://127.0.0.1:3000"


class DokployBootstrapError(RuntimeError):
    """Raised when Dokploy bootstrap cannot reach a locally healthy state."""


@dataclass(frozen=True)
class DokployBootstrapResult:
    outcome: str
    install_command: str
    health_url: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "health_url": self.health_url,
            "install_command": self.install_command,
            "notes": list(self.notes),
            "outcome": self.outcome,
        }


class DokployBootstrapBackend(Protocol):
    def is_healthy(self) -> bool: ...

    def install(self) -> None: ...


class ShellDokployBootstrapBackend:
    """Default subprocess-backed Dokploy bootstrap backend."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        self._forced_health = _optional_bool(raw_env.values, "DOKPLOY_BOOTSTRAP_HEALTHY")
        self._forced_health_after_install = _optional_bool(
            raw_env.values,
            "DOKPLOY_BOOTSTRAP_HEALTHY_AFTER_INSTALL",
        )
        self._forced_install_ok = _optional_bool(raw_env.values, "DOKPLOY_BOOTSTRAP_INSTALL_OK")

    def is_healthy(self) -> bool:
        if self._forced_health is not None:
            return self._forced_health
        return _dokploy_service_present() and _dokploy_http_ready()

    def install(self) -> None:
        if self._forced_install_ok is not None:
            if not self._forced_install_ok:
                msg = "Dokploy bootstrap install command failed."
                raise DokployBootstrapError(msg)
            if self._forced_health_after_install is not None:
                self._forced_health = self._forced_health_after_install
            return

        result = subprocess.run(
            ["sh", "-c", DOKPLOY_INSTALL_COMMAND],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = "Dokploy bootstrap install command failed"
            if stderr:
                msg = f"{msg}: {stderr}"
            raise DokployBootstrapError(msg)


def reconcile_dokploy(
    *,
    dry_run: bool,
    backend: DokployBootstrapBackend,
) -> DokployBootstrapResult:
    if backend.is_healthy():
        return DokployBootstrapResult(
            outcome="already_present",
            install_command=DOKPLOY_INSTALL_COMMAND,
            health_url=LOCAL_HEALTH_URL,
            notes=("Existing local Dokploy health checks already pass.",),
        )

    if dry_run:
        return DokployBootstrapResult(
            outcome="plan_only",
            install_command=DOKPLOY_INSTALL_COMMAND,
            health_url=LOCAL_HEALTH_URL,
            notes=(
                "Dokploy is not locally healthy yet; "
                "the install command would be executed in non-dry-run mode.",
            ),
        )

    backend.install()
    if not _wait_for_health(backend):
        msg = "Dokploy bootstrap did not become locally healthy on http://127.0.0.1:3000."
        raise DokployBootstrapError(msg)

    return DokployBootstrapResult(
        outcome="applied",
        install_command=DOKPLOY_INSTALL_COMMAND,
        health_url=LOCAL_HEALTH_URL,
        notes=("Dokploy install completed and local health checks passed.",),
    )


def _wait_for_health(backend: DokployBootstrapBackend, *, timeout_seconds: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if backend.is_healthy():
            return True
        time.sleep(2.0)
    return backend.is_healthy()


def _dokploy_service_present() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "service", "inspect", "dokploy", "--format", "{{.Spec.Name}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "dokploy"


def _dokploy_http_ready() -> bool:
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 3000, timeout=1.0)
        connection.request("GET", "/")
        response = connection.getresponse()
        return 200 <= response.status < 500
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    msg = f"Invalid boolean value for '{key}': {raw_value!r}."
    raise StateValidationError(msg)
