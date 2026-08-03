"""Local-only finalization for completed Coder service teardown."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Final

from dokploy_wizard.dokploy.coder_secret_destroy_authorization import (
    source_receipt_sha256,
)
from dokploy_wizard.dokploy.coder_secret_destroy_receipts import (
    CoderSecretDestroyReceipt,
    CoderSecretDestroyReceiptError,
    CoderSecretDestroyReceiptStore,
    canonical_destroy_receipt_bytes,
)
from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceiptError,
    CoderSecretReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation_types import now
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationReceipt,
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema import receipt_bytes
from dokploy_wizard.dokploy.coder_service_teardown_contract import (
    CoderServiceTeardownError,
)
from dokploy_wizard.dokploy.coder_service_teardown_receipts import (
    CoderServiceTeardownPhase,
    CoderServiceTeardownReceipt,
    CoderServiceTeardownReceiptError,
    CoderServiceTeardownReceiptStore,
)

_PHASE_ORDER: Final[dict[CoderServiceTeardownPhase, int]] = {
    "intent": 0,
    "service_deleted": 1,
    "source_removed": 2,
    "destroy_removed": 3,
    "workspace_removed": 4,
    "finalized": 5,
}


@dataclass(frozen=True, slots=True)
class CoderServiceTeardownFinalizer:
    state_dir: Path

    def finalize(
        self, receipt: CoderServiceTeardownReceipt
    ) -> CoderServiceTeardownReceipt:
        receipt = self._finalize_source(receipt)
        receipt = self._finalize_destroy(receipt)
        receipt = self._finalize_workspace(receipt)
        return self._advance(receipt, "finalized")

    def _finalize_source(
        self, receipt: CoderServiceTeardownReceipt
    ) -> CoderServiceTeardownReceipt:
        try:
            source = CoderSecretReceiptStore(self.state_dir).load()
            if source is not None:
                if source_receipt_sha256(source) != receipt.source_receipt_sha256:
                    raise CoderServiceTeardownError("Coder secret source receipt drifted")
                CoderSecretReceiptStore(self.state_dir).remove(source)
        except CoderSecretReceiptError as error:
            raise CoderServiceTeardownError(
                "Coder secret source cannot be finalized"
            ) from error
        return self._advance(receipt, "source_removed")

    def _finalize_destroy(
        self, receipt: CoderServiceTeardownReceipt
    ) -> CoderServiceTeardownReceipt:
        try:
            destroy = CoderSecretDestroyReceiptStore(self.state_dir).load()
            if destroy is not None:
                if destroy_receipt_sha256(destroy) != receipt.destroy_receipt_sha256:
                    raise CoderServiceTeardownError("Coder secret destroy receipt drifted")
                CoderSecretDestroyReceiptStore(self.state_dir).remove(destroy)
        except CoderSecretDestroyReceiptError as error:
            raise CoderServiceTeardownError(
                "Coder secret destroy cannot be finalized"
            ) from error
        return self._advance(receipt, "destroy_removed")

    def _finalize_workspace(
        self, receipt: CoderServiceTeardownReceipt
    ) -> CoderServiceTeardownReceipt:
        try:
            workspace = WorkspaceVerificationReceiptStore(self.state_dir).load()
            if receipt.workspace_receipt_sha256 is None:
                if workspace is not None:
                    raise CoderServiceTeardownError("Unexpected Coder workspace receipt")
            elif workspace is not None:
                if workspace_receipt_sha256(workspace) != receipt.workspace_receipt_sha256:
                    raise CoderServiceTeardownError("Coder workspace receipt drifted")
                WorkspaceVerificationReceiptStore(self.state_dir).remove(workspace)
        except CoderSecretClientError as error:
            raise CoderServiceTeardownError(
                "Coder workspace receipt cannot be finalized"
            ) from error
        return self._advance(receipt, "workspace_removed")

    def _advance(
        self,
        receipt: CoderServiceTeardownReceipt,
        phase: CoderServiceTeardownPhase,
    ) -> CoderServiceTeardownReceipt:
        if _PHASE_ORDER[receipt.phase] >= _PHASE_ORDER[phase]:
            return receipt
        updated = replace(receipt, phase=phase, updated_at=now())
        try:
            CoderServiceTeardownReceiptStore(self.state_dir).write(updated)
        except CoderServiceTeardownReceiptError as error:
            raise CoderServiceTeardownError(
                "Coder service teardown receipt cannot be persisted"
            ) from error
        return updated


def destroy_receipt_sha256(receipt: CoderSecretDestroyReceipt) -> str:
    return sha256(canonical_destroy_receipt_bytes(receipt)).hexdigest()


def workspace_receipt_sha256(receipt: WorkspaceVerificationReceipt | None) -> str | None:
    return None if receipt is None else sha256(receipt_bytes(receipt)).hexdigest()
