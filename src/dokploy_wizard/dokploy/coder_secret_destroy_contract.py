"""Typed contracts for Coder secret destroy cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretMetadata
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import WorkspaceVerificationReceipt


@dataclass(frozen=True, slots=True)
class CoderSecretDestroyError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


class CoderSecretDestroyClient(Protocol):
    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]: ...

    def delete_secret(self, secret: CoderSecretMetadata) -> None: ...

    def require_workspace_receipt_absence(self, receipt: WorkspaceVerificationReceipt) -> None: ...
