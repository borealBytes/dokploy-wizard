"""Durable write-ahead state transitions for Task 1 Cloudflare mutations."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_hooks import (
    Task1CloudflareJournalHooks,
    call_hook,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalCleanupState,
    JournalOperationState,
    JournalStatus,
    Task1CloudflareJournalDocument,
    Task1CloudflareJournalIntent,
    Task1CloudflareJournalOperation,
    Task1CloudflareJournalVersion,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_storage import (
    commit_journal_document,
    load_journal_document,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextError,
    Task1ProofContextV1,
    sha256,
)

if TYPE_CHECKING:
    from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_cleanup import (
        Task1CloudflareCleanupBackend,
    )


@dataclass(slots=True)
class Task1CloudflareJournal:
    path: Path
    context_sha256: str
    hooks: Task1CloudflareJournalHooks
    _version: Task1CloudflareJournalVersion | None = None

    @classmethod
    def for_context(
        cls,
        *,
        state_dir: Path,
        context: Task1ProofContextV1,
        hooks: Task1CloudflareJournalHooks | None = None,
    ) -> "Task1CloudflareJournal":
        """Bind a journal path without reading or creating it."""
        return cls(
            path=state_dir / "task1-cloudflare-cleanup.journal.json",
            context_sha256=sha256(context.to_bytes()),
            hooks=hooks or Task1CloudflareJournalHooks(),
        )

    @classmethod
    def open(
        cls,
        *,
        state_dir: Path,
        context: Task1ProofContextV1,
        hooks: Task1CloudflareJournalHooks | None = None,
    ) -> "Task1CloudflareJournal":
        """Open or initialize the one canonical mode-0600 journal for this context."""
        journal = cls.for_context(state_dir=state_dir, context=context, hooks=hooks)
        journal._load(create=True)
        return journal

    def document(self) -> Task1CloudflareJournalDocument:
        return self._load(create=False)

    def existing_operation(
        self, intent: Task1CloudflareJournalIntent
    ) -> Task1CloudflareJournalOperation | None:
        """Read a prior matching intent without creating a journal or changing provider state."""
        if not os.path.lexists(self.path):
            return None
        document = self._load(create=False)
        operation = next(
            (
                candidate
                for candidate in document.operations
                if candidate.intent.logical_key == intent.logical_key
            ),
            None,
        )
        if operation is not None and operation.intent != intent:
            raise Task1ProofContextError("Task 1 Cloudflare journal intent drifted")
        return operation

    def existing_operation_by_logical_key(
        self, logical_key: str
    ) -> Task1CloudflareJournalOperation | None:
        """Read an update intent whose pre-image is expected to differ after submission."""
        if not os.path.lexists(self.path):
            return None
        document = self._load(create=False)
        return next(
            (
                operation
                for operation in document.operations
                if operation.intent.logical_key == logical_key
            ),
            None,
        )

    def record_intent(
        self, intent: Task1CloudflareJournalIntent
    ) -> Task1CloudflareJournalOperation:
        """Fsync one intent before its provider call, or return its resumable prior state."""
        document = self._load(create=True)
        existing = next(
            (
                operation
                for operation in document.operations
                if operation.intent.logical_key == intent.logical_key
            ),
            None,
        )
        if existing is not None:
            if existing.intent != intent or existing.state in {
                JournalOperationState.ABORTED,
                JournalOperationState.CLEANED,
            }:
                raise Task1ProofContextError("Task 1 Cloudflare journal intent drifted")
            return existing
        operation = Task1CloudflareJournalOperation(
            sequence=len(document.operations) + 1,
            intent=intent,
            state=JournalOperationState.INTENT,
            response_id=None,
            post_fingerprint_sha256=None,
            error=None,
            cleanup_state=None,
        )
        self._commit(replace(document, operations=(*document.operations, operation)))
        call_hook(self.hooks.after_intent_fsync, operation)
        return operation

    def record_provider_response(self, operation: Task1CloudflareJournalOperation) -> None:
        call_hook(self.hooks.after_provider_response, operation)

    def checkpoint_created(
        self,
        operation: Task1CloudflareJournalOperation,
        *,
        response_id: str | None,
        post_fingerprint_sha256: str,
    ) -> Task1CloudflareJournalOperation:
        call_hook(self.hooks.before_created_checkpoint, operation)
        document = self._load(create=False)
        if operation.sequence > len(document.operations):
            raise Task1ProofContextError("Task 1 Cloudflare journal creation has no durable intent")
        current = document.operations[operation.sequence - 1]
        if current != operation or current.state is not JournalOperationState.INTENT:
            raise Task1ProofContextError("Task 1 Cloudflare journal creation checkpoint drifted")
        created = replace(
            current,
            state=JournalOperationState.CREATED,
            response_id=response_id,
            post_fingerprint_sha256=post_fingerprint_sha256,
            cleanup_state=JournalCleanupState.PENDING,
        )
        operations = (
            *document.operations[: current.sequence - 1],
            created,
            *document.operations[current.sequence :],
        )
        self._commit(replace(document, operations=operations))
        return created

    def checkpoint_cleaned(
        self, operation: Task1CloudflareJournalOperation, cleanup_state: JournalCleanupState
    ) -> Task1CloudflareJournalOperation:
        document = self._load(create=False)
        current = document.operations[operation.sequence - 1]
        if current != operation or current.state is not JournalOperationState.CREATED:
            raise Task1ProofContextError("Task 1 Cloudflare journal cleanup checkpoint drifted")
        cleaned = replace(current, state=JournalOperationState.CLEANED, cleanup_state=cleanup_state)
        operations = (
            *document.operations[: current.sequence - 1],
            cleaned,
            *document.operations[current.sequence :],
        )
        terminal = all(
            item.state in {JournalOperationState.CLEANED, JournalOperationState.ABORTED}
            for item in operations
        )
        status = (
            JournalStatus.RESTORED
            if terminal and any(item.state is JournalOperationState.ABORTED for item in operations)
            else JournalStatus.CLEANED
            if terminal
            else JournalStatus.ACTIVE
        )
        self._commit(replace(document, status=status, operations=operations))
        call_hook(self.hooks.after_cleanup_checkpoint, cleaned)
        return cleaned

    def checkpoint_aborted(
        self, operation: Task1CloudflareJournalOperation
    ) -> Task1CloudflareJournalOperation:
        document = self._load(create=False)
        current = document.operations[operation.sequence - 1]
        if current != operation or current.state is not JournalOperationState.INTENT:
            raise Task1ProofContextError("Task 1 Cloudflare journal abort checkpoint drifted")
        aborted = replace(
            current,
            state=JournalOperationState.ABORTED,
            error="unresolved-intent",
        )
        operations = (
            *document.operations[: current.sequence - 1],
            aborted,
            *document.operations[current.sequence :],
        )
        terminal = all(
            item.state in {JournalOperationState.ABORTED, JournalOperationState.CLEANED}
            for item in operations
        )
        status = JournalStatus.RESTORED if terminal else JournalStatus.ACTIVE
        self._commit(replace(document, status=status, operations=operations))
        return aborted

    def cleanup(self, backend: "Task1CloudflareCleanupBackend") -> dict[str, str]:
        from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_cleanup import cleanup_journal

        return cleanup_journal(self, backend)

    def _load(self, *, create: bool) -> Task1CloudflareJournalDocument:
        document = load_journal_document(
            path=self.path,
            context_sha256=self.context_sha256,
            expected_version=self._version,
            create=create,
        )
        self._version = document.version
        return document

    def _commit(self, document: Task1CloudflareJournalDocument) -> None:
        self._version = commit_journal_document(
            path=self.path,
            document=document,
            expected_version=self._version,
        )
