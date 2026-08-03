"""Descriptor-authorized transaction record lock."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Literal

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import authorized_parent


class RecordLock:
    def __init__(self, path: Path, trusted_root: Path) -> None:
        self._path = path.with_name(f".{path.name}.lock")
        self._trusted_root = trusted_root
        self._descriptor: int | None = None
        self._stack: ExitStack | None = None

    def __enter__(self) -> None:
        stack = ExitStack()
        authority = stack.enter_context(
            authorized_parent(self._path, trusted_root=self._trusted_root)
        )
        descriptor = os.open(
            authority.name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=authority.descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceCatalogSyncError("workspace transaction lock is invalid")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(authority.descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except (OSError, WorkspaceCatalogSyncError):
            os.close(descriptor)
            stack.close()
            raise
        self._descriptor = descriptor
        self._stack = stack

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exception_type, exception, traceback
        if self._descriptor is None:
            return False
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        if self._stack is not None:
            self._stack.close()
        return False
