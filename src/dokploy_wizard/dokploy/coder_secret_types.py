from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Literal, Protocol

from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretError


class CoderSecretClientError(CoderSecretError):
    """Carries a redacted failure reason while Python attaches traceback state."""

    def __init__(
        self,
        reason: str,
        *,
        kind: Literal[
            "client", "client_workspace_present", "client_workspace_terminal"
        ] = "client",
    ) -> None:
        super().__init__(reason, kind=kind)


class CoderSecretProcessRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        input: str | None,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...
