"""Terminal expectation validation for Task 1 remote proof receipts."""

from __future__ import annotations

from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema import (
    Task1RemoteProofReceipt,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    Task1RemoteProofExpectation,
    Task1RemoteProofResult,
    Task1RemoteReceiptError,
)


def require_terminal_receipt(
    content: bytes, expectation: Task1RemoteProofExpectation
) -> Task1RemoteProofReceipt:
    receipt = Task1RemoteProofReceipt.from_bytes(content)
    if receipt.result is not Task1RemoteProofResult.SUCCESS:
        raise Task1RemoteReceiptError("Task 1 remote proof receipt is not successful")
    if (
        receipt.binding.proof_commit != expectation.proof_commit
        or receipt.binding.context_sha256 != expectation.context_sha256
        or receipt.binding.uploaded_env_sha256 != expectation.uploaded_env_sha256
    ):
        raise Task1RemoteReceiptError("Task 1 remote proof receipt belongs to another invocation")
    return receipt
