"""Host-PID namespace parent identity checks for the external sync helper."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from dokploy_wizard.state.sync_schema import SyncStateError, require_digest


@dataclass(frozen=True, slots=True)
class ParentIdentity:
    pid: int
    start_time_ticks: int
    argv_sha256: str

    def __post_init__(self) -> None:
        if self.pid < 1 or self.start_time_ticks < 0:
            raise SyncStateError("Parent identity is invalid.")
        require_digest(self.argv_sha256, "parent argv")


def read_parent_identity(pid: int, *, proc_root: Path = Path("/proc")) -> ParentIdentity:
    parent = proc_root / str(pid)
    try:
        stat = (parent / "stat").read_text(encoding="utf-8")
        command = (parent / "cmdline").read_bytes()
        suffix = stat.rsplit(") ", 1)[1].split()
        start_time_ticks = int(suffix[19])
    except (FileNotFoundError, IndexError, UnicodeDecodeError, ValueError) as error:
        raise SyncStateError("Parent process identity is unavailable.") from error
    return ParentIdentity(
        pid=pid,
        start_time_ticks=start_time_ticks,
        argv_sha256=sha256(command).hexdigest(),
    )


def verify_parent_identity(
    expected: ParentIdentity, *, proc_root: Path = Path("/proc")
) -> ParentIdentity:
    observed = read_parent_identity(expected.pid, proc_root=proc_root)
    if observed != expected:
        raise SyncStateError("Parent process identity does not match the lease request.")
    return observed
