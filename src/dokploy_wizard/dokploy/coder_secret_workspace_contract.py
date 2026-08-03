from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError


class CoderWorkspaceRunner(Protocol):
    def __call__(self, command: tuple[str, ...]) -> str: ...


class CoderWorkspaceClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class SystemCoderWorkspaceClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class WorkspaceVerificationPolicy:
    timeout_seconds: float = 120.0
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.poll_interval_seconds <= 0:
            raise CoderSecretClientError("Coder workspace verification timing is invalid")


@dataclass(frozen=True, slots=True)
class WorkspaceVerificationIntent:
    owner_id: str
    env_name: str
    expected_value_sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceTemplate:
    template_id: str
    template_name: str


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    workspace_name: str
    owner_id: str
    owner_name: str
    template_id: str
    template_name: str
    status: str


class WorkspaceIdentityError(CoderSecretClientError):
    pass


class WorkspaceHashError(CoderSecretClientError):
    pass
