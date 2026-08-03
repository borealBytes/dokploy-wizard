from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.request import urlopen

from dokploy_wizard.dokploy.workspace_catalog_sync_adapters import adapter_plan
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    ModelCatalog,
    ProcessIdentity,
    TransactionBlockedError,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import (
    ProcessControl,
    observed_process_identity,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_transaction import (
    WorkspaceCatalogTransaction,
)

_HEALTH_TIMEOUT_SECONDS = 5
_HEALTH_POLL_INTERVAL_SECONDS = 0.1
_STOP_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class KdenseProcessReload:
    process: ProcessIdentity
    control: ProcessControl


@dataclass(frozen=True, slots=True)
class KdenseCatalogRefresh:
    file_freshness: Literal["updated", "current"]
    process_freshness: Literal["reloaded", "not-required"]


@dataclass(frozen=True, slots=True)
class KdenseSupervisorControl:
    pid_file: Path
    supervisor: Path
    health_urls: tuple[str, ...]

    def stop(self, pid: int) -> None:
        self.pid_file.unlink(missing_ok=True)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
        while (Path("/proc") / str(pid)).exists():
            if time.monotonic() >= deadline:
                raise WorkspaceCatalogSyncError("K-Dense supervisor did not stop")
            time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)

    def start(self, previous: ProcessIdentity) -> int:
        del previous
        if not self.supervisor.is_file() or self.supervisor.is_symlink():
            raise WorkspaceCatalogSyncError("K-Dense supervisor is unavailable")
        return subprocess.Popen(
            (str(self.supervisor),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ).pid

    def replacement_pid(self, previous: ProcessIdentity) -> int | None:
        pid = _read_pid_file(self.pid_file)
        return None if pid is None or pid == previous.pid else pid

    def verify_health(self) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_SECONDS
        for url in self.health_urls:
            while True:
                ready = False
                try:
                    remaining = max(deadline - time.monotonic(), _HEALTH_POLL_INTERVAL_SECONDS)
                    with urlopen(url, timeout=remaining) as response:
                        ready = response.status == 200
                except (OSError, URLError):
                    ready = False
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise WorkspaceCatalogSyncError("K-Dense health check failed")
                time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)


def load_kdense_process_reload(
    *, workspace_root: Path, pid_file: Path, supervisor: Path
) -> KdenseProcessReload:
    root = workspace_root.resolve(strict=True)
    pid = _read_pid_file(pid_file)
    if pid is None:
        raise WorkspaceCatalogSyncError("K-Dense supervisor PID is unavailable")
    return KdenseProcessReload(
        process=observed_process_identity(
            name="kdense-supervisor", pid=pid, generation=_next_generation(root)
        ),
        control=KdenseSupervisorControl(
            pid_file=pid_file,
            supervisor=supervisor,
            health_urls=("http://127.0.0.1:8000/health", "http://127.0.0.1:3000/health"),
        ),
    )


def refresh_kdense_catalog(
    *,
    workspace_root: Path,
    generation: int,
    catalog: ModelCatalog,
    process_reload: KdenseProcessReload | None,
) -> KdenseCatalogRefresh:
    plan = adapter_plan("kdense", workspace_root, catalog)
    if all(_target_matches(target.path, target.content) for target in plan.targets):
        return KdenseCatalogRefresh("current", "not-required")
    transaction = WorkspaceCatalogTransaction(workspace_root=workspace_root, generation=generation)
    if not any(target.path.exists() or target.path.is_symlink() for target in plan.targets):
        prepared = transaction.prepare(targets=plan.targets)
        transaction.commit(cas_token=prepared.cas_token)
        return KdenseCatalogRefresh("updated", "not-required")
    if process_reload is None:
        raise WorkspaceCatalogSyncError("K-Dense catalog reload needs a process controller")
    prepared = transaction.prepare(
        targets=plan.targets,
        processes=(process_reload.process,),
        explicit_operator_update=True,
    )
    switched = transaction.commit(cas_token=prepared.cas_token)
    try:
        stopped = transaction.stop_processes(
            cas_token=switched.cas_token, control=process_reload.control
        )
        started = transaction.start_processes(
            cas_token=stopped.cas_token, control=process_reload.control
        )
        transaction.verify_health(cas_token=started.cas_token, control=process_reload.control)
    except TransactionBlockedError:
        transaction.resolve_blocked(
            cas_token=transaction.current().cas_token,
            resolution="rollback",
            control=process_reload.control,
        )
        raise
    return KdenseCatalogRefresh("updated", "reloaded")


def _target_matches(path: Path, expected: bytes) -> bool:
    return path.is_file() and not path.is_symlink() and path.read_bytes() == expected


def _read_pid_file(path: Path) -> int | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as error:
        raise WorkspaceCatalogSyncError("K-Dense supervisor PID is invalid") from error
    if pid < 1:
        raise WorkspaceCatalogSyncError("K-Dense supervisor PID is invalid")
    return pid


def _next_generation(root: Path) -> int:
    transactions = root / ".local/state/dokploy-wizard/model-sync/transactions"
    if not transactions.exists():
        return 1
    existing = [
        int(path.name) for path in transactions.iterdir() if path.is_dir() and path.name.isdecimal()
    ]
    return max(existing, default=0) + 1
