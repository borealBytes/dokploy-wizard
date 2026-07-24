"""Cross-field state-machine validation for Task 1 Cloudflare journal documents."""

from __future__ import annotations

from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalCleanupState,
    JournalOperationKind,
    JournalOperationState,
    JournalStatus,
    Task1CloudflareJournalDocument,
    Task1CloudflareJournalOperation,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError


def validate_document(document: Task1CloudflareJournalDocument) -> None:
    """Reject duplicate identities, illegal transitions, and inconsistent terminal state."""
    sequences = tuple(operation.sequence for operation in document.operations)
    keys = tuple(operation.intent.logical_key for operation in document.operations)
    if sequences != tuple(range(1, len(sequences) + 1)) or len(keys) != len(set(keys)):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    for operation in document.operations:
        _validate_operation(operation)
    all_cleaned = bool(document.operations) and all(
        operation.state is JournalOperationState.CLEANED for operation in document.operations
    )
    any_aborted = any(
        operation.state is JournalOperationState.ABORTED for operation in document.operations
    )
    terminal = all_cleaned or (
        bool(document.operations)
        and all(
            operation.state in {JournalOperationState.CLEANED, JournalOperationState.ABORTED}
            for operation in document.operations
        )
        and any_aborted
    )
    if (document.status is JournalStatus.CLEANED) != all_cleaned or (
        document.status is JournalStatus.RESTORED
    ) != (terminal and any_aborted):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")


def _validate_operation(operation: Task1CloudflareJournalOperation) -> None:
    intent = operation.intent
    if intent.kind is JournalOperationKind.TUNNEL_CONFIGURATION and intent.parent_id is None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    if intent.kind is JournalOperationKind.ACCESS_POLICY and intent.parent_id is None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    if intent.kind is not JournalOperationKind.TUNNEL_CONFIGURATION and (
        intent.expected_name_sha256 is None and intent.expected_domain_sha256 is None
    ):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    if operation.state is JournalOperationState.INTENT and any(
        value is not None
        for value in (
            operation.response_id,
            operation.post_fingerprint_sha256,
            operation.error,
            operation.cleanup_state,
        )
    ):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    if operation.state is JournalOperationState.ABORTED and (
        operation.error != "unresolved-intent"
        or any(
            value is not None
            for value in (
                operation.response_id,
                operation.post_fingerprint_sha256,
                operation.cleanup_state,
            )
        )
    ):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    if operation.state is JournalOperationState.CREATED and (
        operation.post_fingerprint_sha256 is None
        or operation.cleanup_state is not JournalCleanupState.PENDING
        or operation.error is not None
        or (
            operation.intent.kind is not JournalOperationKind.TUNNEL_CONFIGURATION
            and operation.response_id is None
        )
    ):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    if operation.state is JournalOperationState.CLEANED and operation.cleanup_state not in {
        JournalCleanupState.VERIFIED_ABSENT,
        JournalCleanupState.DELETED_WITH_TUNNEL,
    }:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
