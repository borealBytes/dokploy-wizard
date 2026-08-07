from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Protocol

from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretError


class CoderSecretClientError(CoderSecretError):
    """Carries a redacted failure reason while Python attaches traceback state."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, kind="client")


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
