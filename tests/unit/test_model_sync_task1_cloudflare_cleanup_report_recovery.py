from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import assert_never

import pytest

from dokploy_wizard.networking.cloudflare import CloudflareTunnel
from dokploy_wizard.proof import model_sync_task1_cloudflare_cleanup_report as cleanup_report
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import tunnel_fingerprint
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import parse_cleanup_report
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import CloudflareSnapshotV1
from tests.unit._model_sync_task1_cloudflare_journal_cleanup_support import CleanupBackend
from tests.unit._model_sync_task1_cloudflare_journal_common import _context, _intent
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import snapshot


class InjectedCrash(RuntimeError):
    pass


def _report_bytes(report: dict[str, JsonValue]) -> bytes:
    return json.dumps(report, sort_keys=True, separators=(",", ":")).encode()


def _record_tunnel(journal: Task1CloudflareJournal, backend: CleanupBackend) -> None:
    tunnel: CloudflareTunnel = backend.tunnels["tunnel-1"]
    operation = journal.record_intent(
        _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1-tunnel"})
    )
    journal.checkpoint_created(
        operation,
        response_id="tunnel-1",
        post_fingerprint_sha256=tunnel_fingerprint(tunnel),
    )


@pytest.mark.parametrize(
    "target",
    [
        cleanup_report.Task1CloudflareCleanupBoundary.JOURNAL_CLEANED,
        cleanup_report.Task1CloudflareCleanupBoundary.POST_CLEANUP_SNAPSHOT_PUBLISHED,
        cleanup_report.Task1CloudflareCleanupBoundary.RECEIPT_VALIDATED,
    ],
)
def test_cleanup_report_recovers_each_late_crash_without_duplicate_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: cleanup_report.Task1CloudflareCleanupBoundary,
) -> None:
    # Given
    # This crash-boundary test intentionally isolates only post-validation crashes.
    monkeypatch.setattr(cleanup_report, "validate_install_delta", lambda *_args: None)
    context = _context(tmp_path)
    context_sha256 = hashlib.sha256(context.to_bytes()).hexdigest()
    scope = CloudflareSnapshotScope(context_sha256, "account-a", "zone-a")
    pre_install = snapshot(context_sha256, tunnel=False)
    post_install = snapshot(context_sha256, tunnel=True)
    (tmp_path / "task1-cloudflare-pre-install.snapshot.json").write_bytes(pre_install.to_bytes())
    (tmp_path / "task1-cloudflare-pre-install.snapshot.json").chmod(0o600)
    backend = CleanupBackend()
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=context)
    _record_tunnel(journal, backend)
    captures: list[str] = []

    def capture() -> CloudflareSnapshotV1:
        captures.append("post" if backend.tunnels else "cleanup")
        return post_install if backend.tunnels else pre_install

    def interrupt(boundary: cleanup_report.Task1CloudflareCleanupBoundary) -> None:
        if boundary is target:
            raise InjectedCrash(target.value)

    # When
    with pytest.raises(InjectedCrash, match=target.value):
        cleanup_report.run_task1_cloudflare_cleanup(
            cleanup_report.Task1CloudflareCleanupRun(
                state_dir=tmp_path,
                scope=scope,
                journal=journal,
                backend=backend,
                capture_snapshot=capture,
            ),
            boundary_hook=interrupt,
        )

    # Then
    assert journal.document().status.value == "cleaned"
    assert backend.deleted == ["tunnel:tunnel-1"]
    match target:
        case cleanup_report.Task1CloudflareCleanupBoundary.JOURNAL_CLEANED:
            assert not (tmp_path / "task1-cloudflare-post-cleanup.snapshot.json").exists()
        case cleanup_report.Task1CloudflareCleanupBoundary.POST_CLEANUP_SNAPSHOT_PUBLISHED:
            assert (tmp_path / "task1-cloudflare-post-cleanup.snapshot.json").exists()
        case cleanup_report.Task1CloudflareCleanupBoundary.RECEIPT_VALIDATED:
            assert (tmp_path / "task1-cloudflare-post-cleanup.snapshot.json").exists()
        case unexpected:
            assert_never(unexpected)

    recovered = cleanup_report.run_task1_cloudflare_cleanup(
        cleanup_report.Task1CloudflareCleanupRun(
            state_dir=tmp_path,
            scope=scope,
            journal=Task1CloudflareJournal.for_context(state_dir=tmp_path, context=context),
            backend=backend,
            capture_snapshot=capture,
        ),
    )
    recovered_bytes = _report_bytes(recovered)
    rerun = cleanup_report.run_task1_cloudflare_cleanup(
        cleanup_report.Task1CloudflareCleanupRun(
            state_dir=tmp_path,
            scope=scope,
            journal=Task1CloudflareJournal.for_context(state_dir=tmp_path, context=context),
            backend=backend,
            capture_snapshot=capture,
        ),
    )

    assert backend.deleted == ["tunnel:tunnel-1"]
    assert captures == ["post", "cleanup"]
    assert rerun == recovered
    assert _report_bytes(rerun) == recovered_bytes
    assert parse_cleanup_report(_report_bytes(rerun)) == parse_cleanup_report(recovered_bytes)
