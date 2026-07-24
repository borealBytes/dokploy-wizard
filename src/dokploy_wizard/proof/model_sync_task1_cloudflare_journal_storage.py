"""Atomic bounded storage for the private Task 1 Cloudflare journal."""

from __future__ import annotations

import os
import secrets
from dataclasses import replace
from pathlib import Path

from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof import read_bounded_regular_bytes
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JOURNAL_MAX_BYTES,
    JournalStatus,
    Task1CloudflareJournalDocument,
    Task1CloudflareJournalVersion,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError


def load_journal_document(
    *,
    path: Path,
    context_sha256: str,
    expected_version: Task1CloudflareJournalVersion | None,
    create: bool,
) -> Task1CloudflareJournalDocument:
    if not os.path.lexists(path):
        if not create:
            raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is absent")
        document = Task1CloudflareJournalDocument(
            context_sha256=context_sha256,
            version=Task1CloudflareJournalVersion(0, secrets.token_hex(32)),
            status=JournalStatus.ACTIVE,
            operations=(),
        )
        artifacts.atomic_write_bytes(path, document.to_bytes(), mode=0o600)
    document = _read_journal_document(path)
    if document.context_sha256 != context_sha256:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal context mismatch")
    if expected_version is not None and document.version != expected_version:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal generation/CAS drifted")
    return document


def commit_journal_document(
    *,
    path: Path,
    document: Task1CloudflareJournalDocument,
    expected_version: Task1CloudflareJournalVersion | None,
) -> Task1CloudflareJournalVersion:
    if expected_version is None or document.version != expected_version:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal generation/CAS drifted")
    if _read_journal_document(path).version != expected_version:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal generation/CAS drifted")
    committed = replace(
        document,
        version=Task1CloudflareJournalVersion(
            expected_version.generation + 1, secrets.token_hex(32)
        ),
    )
    Task1CloudflareJournalDocument.from_bytes(committed.to_bytes())
    artifacts.atomic_write_bytes(path, committed.to_bytes(), mode=0o600)
    return committed.version


def _read_journal_document(path: Path) -> Task1CloudflareJournalDocument:
    try:
        content, _mode = read_bounded_regular_bytes(path, JOURNAL_MAX_BYTES, 0o600)
        return Task1CloudflareJournalDocument.from_bytes(content)
    except (OSError, ValueError) as error:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is unreadable") from error
