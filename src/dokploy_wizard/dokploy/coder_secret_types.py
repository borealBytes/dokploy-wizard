from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Literal, Protocol

from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretError

CoderSecretClientFailureKind = Literal[
    "client_command_failed",
    "client_command_timeout",
    "client_env_binding",
    "client_invalid_operation",
    "client_metadata_invalid",
    "client_output_limit",
    "client_workspace_cleanup",
    "client_workspace_create",
    "client_workspace_delete",
    "client_workspace_hash",
    "client_workspace_identity",
    "client_workspace_intent",
    "client_workspace_inventory",
    "client_workspace_policy",
    "client_workspace_present",
    "client_workspace_readiness",
    "client_workspace_receipt_invalid",
    "client_workspace_receipt_read",
    "client_workspace_receipt_state",
    "client_workspace_receipt_write",
    "client_workspace_template",
    "client_workspace_template_legacy_ambiguous",
    "client_workspace_template_payload",
    "client_workspace_template_primary_absent",
    "client_workspace_template_primary_ambiguous",
    "client_workspace_template_record_invalid",
    "client_workspace_template_root",
    "client_workspace_terminal",
]


class CoderSecretClientError(CoderSecretError):
    """Carries a redacted failure reason while Python attaches traceback state."""

    def __init__(
        self,
        reason: str,
        *,
        kind: CoderSecretClientFailureKind,
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
