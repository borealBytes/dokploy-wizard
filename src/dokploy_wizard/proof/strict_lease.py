"""Process-backed lease for strict proof mutation accounting."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dokploy_wizard.proof.mutation_registry import (
    MutationInventory,
    ProofMutationRecorder,
    StrictLeaseNotHeldError,
    _StrictLeaseCapability,
)


@contextmanager
def strict_proof_lease(lock_path: Path) -> Iterator[ProofMutationRecorder]:
    """Hold a mode-0600 nonblocking OS lease for one strict proof pass."""

    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
            raise StrictLeaseNotHeldError()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StrictLeaseNotHeldError() from error
        acquired = True
        os.fchmod(descriptor, 0o600)
        capability = _StrictLeaseCapability()
        recorder = ProofMutationRecorder(MutationInventory.required(), capability)
        try:
            yield recorder
        finally:
            capability.active = False
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
