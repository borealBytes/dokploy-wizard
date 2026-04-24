"""Workspace-scoped Multica daemon registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

HEARTBEAT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class MulticaWorkspaceRuntime:
    workspace_id: str
    daemon_url: str
    token: str


@dataclass
class _WorkspaceDaemonState:
    daemon_url: str
    token: str
    last_heartbeat_at: float


class MulticaDaemonRegistry:
    def __init__(
        self,
        *,
        time_fn: Callable[[], float] | None = None,
        heartbeat_timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> None:
        self._time_fn = monotonic if time_fn is None else time_fn
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._workspace_daemons: dict[str, _WorkspaceDaemonState] = {}

    def register_workspace(self, workspace_id: str, daemon_url: str, token: str) -> None:
        self._workspace_daemons[workspace_id] = _WorkspaceDaemonState(
            daemon_url=daemon_url,
            token=token,
            last_heartbeat_at=self._time_fn(),
        )

    def heartbeat(self, workspace_id: str) -> None:
        workspace = self._workspace_daemons[workspace_id]
        workspace.last_heartbeat_at = self._time_fn()

    def get_available_runtimes(self) -> tuple[MulticaWorkspaceRuntime, ...]:
        now = self._time_fn()
        available_runtimes = [
            MulticaWorkspaceRuntime(
                workspace_id=workspace_id,
                daemon_url=workspace.daemon_url,
                token=workspace.token,
            )
            for workspace_id, workspace in sorted(self._workspace_daemons.items())
            if now - workspace.last_heartbeat_at < self._heartbeat_timeout_seconds
        ]
        return tuple(available_runtimes)
