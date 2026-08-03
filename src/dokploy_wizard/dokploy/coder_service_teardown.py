"""Crash-safe Coder service teardown and local receipt finalization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from dokploy_wizard.dokploy.coder_secret_destroy_authorization import (
    completed_source_receipt,
    secret_id,
    source_receipt_sha256,
)
from dokploy_wizard.dokploy.coder_secret_destroy_contract import CoderSecretDestroyError
from dokploy_wizard.dokploy.coder_secret_destroy_receipts import (
    CoderSecretDestroyReceipt,
    CoderSecretDestroyReceiptError,
    CoderSecretDestroyReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_receipts import CoderSecretReceipt
from dokploy_wizard.dokploy.coder_secret_reconciliation_types import now
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPhase,
    WorkspaceVerificationReceipt,
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.dokploy.coder_service_teardown_contract import (
    CoderServiceTeardownBinding,
    CoderServiceTeardownError,
)
from dokploy_wizard.dokploy.coder_service_teardown_finalizer import (
    CoderServiceTeardownFinalizer,
    destroy_receipt_sha256,
    workspace_receipt_sha256,
)
from dokploy_wizard.dokploy.coder_service_teardown_receipts import (
    CoderServiceTeardownReceipt,
    CoderServiceTeardownReceiptError,
    CoderServiceTeardownReceiptStore,
)


@dataclass(frozen=True, slots=True)
class CoderServiceTeardown:
    state_dir: Path
    binding: CoderServiceTeardownBinding

    def current(self) -> CoderServiceTeardownReceipt | None:
        try:
            receipt = CoderServiceTeardownReceiptStore(self.state_dir).load()
            if receipt is not None:
                self._require_binding(receipt)
                if receipt.phase == "intent":
                    self._require_intent_receipts(receipt)
        except (
            CoderSecretDestroyError,
            CoderSecretDestroyReceiptError,
            CoderSecretClientError,
            CoderServiceTeardownReceiptError,
        ) as error:
            raise CoderServiceTeardownError(
                "Coder service teardown receipt cannot be loaded"
            ) from error
        return receipt

    def begin(self) -> CoderServiceTeardownReceipt:
        store = CoderServiceTeardownReceiptStore(self.state_dir)
        try:
            existing = store.load()
            if existing is not None:
                self._require_binding(existing)
                if existing.phase == "intent":
                    self._require_intent_receipts(existing)
                return existing
            source = completed_source_receipt(self.state_dir)
            if source.owner_id != self.binding.owner_id:
                raise CoderServiceTeardownError("Coder secret source owner is invalid")
            destroy = self._completed_destroy(source)
            workspace = self._terminal_workspace(source.owner_id)
            receipt = CoderServiceTeardownReceipt(
                stack_name=self.binding.stack_name,
                resource_type=self.binding.resource_type,
                resource_id=self.binding.resource_id,
                resource_scope=self.binding.resource_scope,
                owner_id=source.owner_id,
                source_receipt_sha256=source_receipt_sha256(source),
                destroy_receipt_sha256=destroy_receipt_sha256(destroy),
                workspace_receipt_sha256=workspace_receipt_sha256(workspace),
                phase="intent",
                updated_at=now(),
            )
            store.write(receipt)
            return receipt
        except (
            CoderSecretDestroyReceiptError,
            CoderSecretDestroyError,
            CoderSecretClientError,
            CoderServiceTeardownReceiptError,
        ) as error:
            raise CoderServiceTeardownError(
                "Coder service teardown receipt cannot begin"
            ) from error

    def complete(self) -> CoderServiceTeardownReceipt:
        receipt = self._required_receipt()
        self._require_binding(receipt)
        if receipt.phase != "intent":
            return receipt
        completed = replace(receipt, phase="service_deleted", updated_at=now())
        self._write(completed)
        return completed

    def finalize(self) -> CoderServiceTeardownReceipt:
        receipt = self._required_receipt()
        self._require_binding(receipt)
        if receipt.phase == "intent":
            raise CoderServiceTeardownError(
                "Coder service deletion is not completed for finalization"
            )
        return CoderServiceTeardownFinalizer(self.state_dir).finalize(receipt)

    def finish(self) -> None:
        receipt = self._required_receipt()
        self._require_binding(receipt)
        if receipt.phase != "finalized":
            raise CoderServiceTeardownError("Coder service teardown is not finalized")
        try:
            CoderServiceTeardownReceiptStore(self.state_dir).remove(receipt)
        except CoderServiceTeardownReceiptError as error:
            raise CoderServiceTeardownError(
                "Coder service teardown receipt cannot be removed"
            ) from error

    @staticmethod
    def finish_orphaned(state_dir: Path) -> None:
        try:
            receipt = CoderServiceTeardownReceiptStore(state_dir).load()
        except CoderServiceTeardownReceiptError as error:
            raise CoderServiceTeardownError(
                "Coder service teardown receipt cannot be loaded"
            ) from error
        if receipt is None:
            return
        if receipt.phase != "finalized":
            raise CoderServiceTeardownError("Coder service teardown is incomplete")
        CoderServiceTeardown(
            state_dir,
            CoderServiceTeardownBinding(
                receipt.stack_name,
                receipt.resource_type,
                receipt.resource_id,
                receipt.resource_scope,
                receipt.owner_id,
            ),
        ).finish()

    def _completed_destroy(self, source: CoderSecretReceipt) -> CoderSecretDestroyReceipt:
        destroy = CoderSecretDestroyReceiptStore(self.state_dir).load()
        if destroy is None or destroy.status != "completed":
            raise CoderServiceTeardownError("Coder secret destroy proof is not completed")
        if destroy.owner_id != source.owner_id:
            raise CoderServiceTeardownError("Coder secret destroy owner is invalid")
        if destroy.source_receipt_sha256 != source_receipt_sha256(source):
            raise CoderServiceTeardownError("Coder secret destroy source binding is invalid")
        expected = {
            (secret_id(step), step.secret_name, step.env_name, step.description)
            for step in source.steps
        }
        observed = {
            (step.secret_id, step.secret_name, step.env_name, step.description)
            for step in destroy.steps
        }
        if observed != expected:
            raise CoderServiceTeardownError("Coder secret destroy inventory is invalid")
        return destroy

    def _terminal_workspace(self, owner_id: str) -> WorkspaceVerificationReceipt | None:
        workspace = WorkspaceVerificationReceiptStore(self.state_dir).load()
        if workspace is None:
            return None
        if (
            workspace.owner_id != owner_id
            or workspace.phase is not WorkspaceVerificationPhase.DELETED
        ):
            raise CoderServiceTeardownError("Coder workspace teardown proof is invalid")
        return workspace

    def _require_intent_receipts(self, receipt: CoderServiceTeardownReceipt) -> None:
        source = completed_source_receipt(self.state_dir)
        if (
            source.owner_id != receipt.owner_id
            or source_receipt_sha256(source) != receipt.source_receipt_sha256
        ):
            raise CoderServiceTeardownError("Coder secret source receipt changed after intent")
        destroy = self._completed_destroy(source)
        if destroy_receipt_sha256(destroy) != receipt.destroy_receipt_sha256:
            raise CoderServiceTeardownError("Coder secret destroy receipt changed after intent")
        workspace = self._terminal_workspace(receipt.owner_id)
        if workspace_receipt_sha256(workspace) != receipt.workspace_receipt_sha256:
            raise CoderServiceTeardownError("Coder workspace receipt changed after intent")

    def _required_receipt(self) -> CoderServiceTeardownReceipt:
        try:
            receipt = CoderServiceTeardownReceiptStore(self.state_dir).load()
        except CoderServiceTeardownReceiptError as error:
            raise CoderServiceTeardownError(
                "Coder service teardown receipt cannot be loaded"
            ) from error
        if receipt is None:
            raise CoderServiceTeardownError("Coder service teardown receipt is missing")
        return receipt

    def _write(self, receipt: CoderServiceTeardownReceipt) -> None:
        try:
            CoderServiceTeardownReceiptStore(self.state_dir).write(receipt)
        except CoderServiceTeardownReceiptError as error:
            raise CoderServiceTeardownError(
                "Coder service teardown receipt cannot be persisted"
            ) from error

    def _require_binding(self, receipt: CoderServiceTeardownReceipt) -> None:
        if (
            receipt.stack_name != self.binding.stack_name
            or receipt.resource_type != self.binding.resource_type
            or receipt.resource_id != self.binding.resource_id
            or receipt.resource_scope != self.binding.resource_scope
            or receipt.owner_id != self.binding.owner_id
        ):
            raise CoderServiceTeardownError("Coder service teardown binding is invalid")
__all__ = [
    "CoderServiceTeardown",
    "CoderServiceTeardownBinding",
    "CoderServiceTeardownError",
]
