from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError
from tests.unit._model_sync_task1_cloudflare_journal_common import _context, _intent, opaque_hash


def test_journal_rejects_noncanonical_unknown_and_oversized_bytes(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    journal.path.write_bytes(
        b'{"context_sha256":"'
        + journal.context_sha256.encode()
        + b'","operations":[],"schema_version":1,"unknown":true}\n'
    )

    with pytest.raises(Task1ProofContextError, match="unreadable|invalid"):
        journal.record_intent(
            _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1"})
        )

    journal.path.write_bytes(b"x" * (256 * 1024 + 1))

    with pytest.raises(Task1ProofContextError, match="unreadable|invalid"):
        journal.record_intent(
            _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1"})
        )


def test_journal_rejects_symlink_and_generation_cas_drift(tmp_path: Path) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    target = tmp_path / "foreign.json"
    target.write_bytes(journal.path.read_bytes())
    journal.path.unlink()
    journal.path.symlink_to(target)

    with pytest.raises(Task1ProofContextError, match="unreadable|invalid"):
        journal.record_intent(
            _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1"})
        )


def test_journal_rejects_wrong_mode_and_checkpoint_generation_drift(tmp_path: Path) -> None:
    mode_journal = Task1CloudflareJournal.open(
        state_dir=tmp_path / "mode", context=_context(tmp_path)
    )
    mode_journal.path.chmod(0o640)

    with pytest.raises(Task1ProofContextError, match="unreadable"):
        mode_journal.document()

    journal = Task1CloudflareJournal.open(state_dir=tmp_path / "cas", context=_context(tmp_path))
    operation = journal.record_intent(
        _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1"})
    )
    document = journal.document()
    tampered = replace(
        document,
        version=replace(document.version, generation=document.version.generation + 1),
    )
    journal.path.write_bytes(tampered.to_bytes())

    with pytest.raises(Task1ProofContextError, match="generation/CAS"):
        journal.checkpoint_created(
            operation,
            response_id="tunnel-1",
            post_fingerprint_sha256=opaque_hash("post"),
        )

    journal.path.unlink()
    journal.path.write_bytes(
        b'{"cas_token":"'
        + b"0" * 64
        + b'","context_sha256":"'
        + journal.context_sha256.encode()
        + b'","generation":1,"operations":[],"schema_version":1,"status":"active"}\n'
    )

    with pytest.raises(Task1ProofContextError, match="unreadable|invalid"):
        journal.record_intent(
            _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1"})
        )
