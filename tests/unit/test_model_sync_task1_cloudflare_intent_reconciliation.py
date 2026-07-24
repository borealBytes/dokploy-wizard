from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_task1_cloudflare_intent_reconciliation import (
    _resolve_configuration,
    _resolve_resources,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    JournalOperationState,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
    CloudflareSnapshotCollectionError,
)
from tests.unit._model_sync_task1_cloudflare_journal_common import _context, _intent
from tests.unit._model_sync_task1_cloudflare_journal_crash_safety_support import (
    _configuration_intent,
    _ConfigurationBackend,
)


@pytest.mark.parametrize(
    ("candidates", "state"),
    [
        ((), JournalOperationState.ABORTED),
        ((("tunnel-1", "d" * 64),), JournalOperationState.CREATED),
    ],
)
def test_resource_intent_resolves_only_absent_or_one_exact_candidate(
    tmp_path: Path,
    candidates: tuple[tuple[str, str], ...],
    state: JournalOperationState,
) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    intent = _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1"})
    intent = intent.__class__(
        intent.logical_key,
        intent.kind,
        intent.account_id,
        intent.zone_id,
        intent.parent_id,
        intent.expected_name_sha256,
        intent.expected_domain_sha256,
        intent.pre_absence_sha256,
        "d" * 64,
    )
    operation = journal.record_intent(intent)

    _resolve_resources(journal, operation, candidates)

    assert journal.document().operations[0].state is state


@pytest.mark.parametrize(
    "candidates",
    [(("tunnel-1", "c" * 64),), (("one", "d" * 64), ("two", "d" * 64))],
)
def test_resource_intent_preserves_unresolved_state_for_drift_or_ambiguity(
    tmp_path: Path, candidates: tuple[tuple[str, str], ...]
) -> None:
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    intent = _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1"})
    intent = intent.__class__(
        intent.logical_key,
        intent.kind,
        intent.account_id,
        intent.zone_id,
        intent.parent_id,
        intent.expected_name_sha256,
        intent.expected_domain_sha256,
        intent.pre_absence_sha256,
        "d" * 64,
    )
    operation = journal.record_intent(intent)

    with pytest.raises(CloudflareSnapshotCollectionError):
        _resolve_resources(journal, operation, candidates)

    assert journal.document().operations[0].state is JournalOperationState.INTENT


@pytest.mark.parametrize("current", ["pre", "desired"])
def test_configuration_intent_resolves_only_preimage_or_desired(
    tmp_path: Path, current: str
) -> None:
    pre_image = ({"service": "pre"},)
    desired = ({"service": "desired"},)
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    operation = journal.record_intent(_configuration_intent(pre_image, desired))
    backend = _ConfigurationBackend(({"service": current},))

    _resolve_configuration(journal, backend, operation, frozenset({"tunnel-1"}))

    expected = JournalOperationState.ABORTED if current == "pre" else JournalOperationState.CREATED
    assert journal.document().operations[0].state is expected


def test_configuration_intent_rejects_third_state(tmp_path: Path) -> None:
    pre_image = ({"service": "pre"},)
    desired = ({"service": "desired"},)
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    operation = journal.record_intent(_configuration_intent(pre_image, desired))

    with pytest.raises(CloudflareSnapshotCollectionError):
        _resolve_configuration(
            journal,
            _ConfigurationBackend(({"service": "drift"},)),
            operation,
            frozenset({"tunnel-1"}),
        )

    assert journal.document().operations[0].state is JournalOperationState.INTENT


def test_configuration_intent_rejects_missing_parent_before_preimage_abort(tmp_path: Path) -> None:
    pre_image = ({"service": "pre"},)
    desired = ({"service": "desired"},)
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=_context(tmp_path))
    operation = journal.record_intent(_configuration_intent(pre_image, desired))

    with pytest.raises(CloudflareSnapshotCollectionError, match="parent is missing"):
        _resolve_configuration(
            journal,
            _ConfigurationBackend(pre_image),
            operation,
            frozenset(),
        )

    assert journal.document().operations[0].state is JournalOperationState.INTENT
