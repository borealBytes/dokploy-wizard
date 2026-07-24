from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dokploy_wizard.proof import model_sync_task1_cloudflare_cleanup_report as cleanup_report
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    snapshot_value_hash,
    tunnel_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_intent_reconciliation import (
    _resolve_resources,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_delta import (
    CloudflareSnapshotDeltaError,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    CloudflareSnapshotReportError,
    parse_cleanup_report,
    parse_restoration_report,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotResourceV1,
    CloudflareSnapshotV1,
)
from tests.unit._model_sync_task1_cloudflare_journal_cleanup_support import CleanupBackend
from tests.unit._model_sync_task1_cloudflare_journal_common import _context, _intent
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    snapshot,
    snapshot_with_additions,
)


def _post_install_snapshot(
    context_sha256: str, backend: CleanupBackend, *, include_aborted_dns: bool
) -> CloudflareSnapshotV1:
    tunnel = backend.tunnels["tunnel-1"]
    additions = [
        CloudflareSnapshotResourceV1(
            "tunnel",
            snapshot_value_hash(tunnel.tunnel_id),
            {
                "configuration_availability": "read",
                "configuration_sha256": "a" * 64,
                "config_src": "cloudflare",
                "name_sha256": snapshot_value_hash(tunnel.name),
                "spec_sha256": tunnel_fingerprint(tunnel),
                "status": "healthy",
            },
        )
    ]
    if include_aborted_dns:
        additions.append(
            CloudflareSnapshotResourceV1(
                "dns_record",
                snapshot_value_hash("late-third-party-dns"),
                {
                    "content_sha256": "b" * 64,
                    "name_sha256": snapshot_value_hash("absent.example.com"),
                    "proxied": True,
                    "record_type": "CNAME",
                    "spec_sha256": "c" * 64,
                },
            )
        )
    return snapshot_with_additions(context_sha256, tuple(additions))


def _journal(
    tmp_path: Path, backend: CleanupBackend, *, abort_dns: bool
) -> tuple[Task1CloudflareJournal, str]:
    context = _context(tmp_path)
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=context)
    tunnel = journal.record_intent(
        _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": "task1-tunnel"})
    )
    journal.checkpoint_created(
        tunnel,
        response_id="tunnel-1",
        post_fingerprint_sha256=tunnel_fingerprint(backend.tunnels["tunnel-1"]),
    )
    if abort_dns:
        absent = journal.record_intent(
            _intent(
                JournalOperationKind.DNS_RECORD,
                {"account_id": "account", "zone_id": "zone", "hostname": "absent.example.com"},
            )
        )
        _resolve_resources(journal, absent, ())
    return journal, hashlib.sha256(context.to_bytes()).hexdigest()


def test_cleanup_report_restores_created_tunnel_after_verified_aborted_dns(tmp_path: Path) -> None:
    # Given
    backend = CleanupBackend()
    journal, context_sha256 = _journal(tmp_path, backend, abort_dns=True)
    scope = CloudflareSnapshotScope(context_sha256, "account-a", "zone-a")
    pre_install = snapshot(context_sha256, tunnel=False)
    post_install = _post_install_snapshot(context_sha256, backend, include_aborted_dns=False)
    pre_path = tmp_path / "task1-cloudflare-pre-install.snapshot.json"
    pre_path.write_bytes(pre_install.to_bytes())
    pre_path.chmod(0o600)
    captures: list[str] = []

    def capture() -> CloudflareSnapshotV1:
        captures.append("post-install" if backend.tunnels else "post-cleanup")
        return post_install if backend.tunnels else pre_install

    # When
    report = cleanup_report.run_task1_cloudflare_cleanup(
        cleanup_report.Task1CloudflareCleanupRun(tmp_path, scope, journal, backend, capture)
    )
    rerun = cleanup_report.run_task1_cloudflare_cleanup(
        cleanup_report.Task1CloudflareCleanupRun(
            tmp_path,
            scope,
            Task1CloudflareJournal.for_context(state_dir=tmp_path, context=_context(tmp_path)),
            backend,
            capture,
        )
    )

    # Then
    content = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    restored = parse_restoration_report(content)
    assert report["status"] == "restored"
    assert rerun == report
    assert backend.deleted == ["tunnel:tunnel-1"]
    assert captures == ["post-install", "post-cleanup"]
    assert restored.pre_install == restored.post_cleanup
    with pytest.raises(CloudflareSnapshotReportError):
        parse_cleanup_report(content)


def test_cleanup_report_rejects_late_resource_matching_aborted_dns_before_deletion(
    tmp_path: Path,
) -> None:
    # Given
    backend = CleanupBackend()
    journal, context_sha256 = _journal(tmp_path, backend, abort_dns=True)
    scope = CloudflareSnapshotScope(context_sha256, "account-a", "zone-a")
    pre_install = snapshot(context_sha256, tunnel=False)
    post_install = _post_install_snapshot(context_sha256, backend, include_aborted_dns=True)
    pre_path = tmp_path / "task1-cloudflare-pre-install.snapshot.json"
    pre_path.write_bytes(pre_install.to_bytes())
    pre_path.chmod(0o600)

    # When / Then
    with pytest.raises(CloudflareSnapshotDeltaError, match="aborted resource"):
        cleanup_report.run_task1_cloudflare_cleanup(
            cleanup_report.Task1CloudflareCleanupRun(
                tmp_path, scope, journal, backend, lambda: post_install
            )
        )
    assert backend.deleted == []
    assert "tunnel-1" in backend.tunnels


def test_cleanup_report_revalidates_cleaned_terminal_journal(tmp_path: Path) -> None:
    # Given
    backend = CleanupBackend()
    journal, context_sha256 = _journal(tmp_path, backend, abort_dns=False)
    scope = CloudflareSnapshotScope(context_sha256, "account-a", "zone-a")
    pre_install = snapshot(context_sha256, tunnel=False)
    post_install = _post_install_snapshot(context_sha256, backend, include_aborted_dns=False)
    pre_path = tmp_path / "task1-cloudflare-pre-install.snapshot.json"
    pre_path.write_bytes(pre_install.to_bytes())
    pre_path.chmod(0o600)

    def capture() -> CloudflareSnapshotV1:
        return post_install if backend.tunnels else pre_install

    # When
    report = cleanup_report.run_task1_cloudflare_cleanup(
        cleanup_report.Task1CloudflareCleanupRun(tmp_path, scope, journal, backend, capture)
    )
    rerun = cleanup_report.run_task1_cloudflare_cleanup(
        cleanup_report.Task1CloudflareCleanupRun(
            tmp_path,
            scope,
            Task1CloudflareJournal.for_context(state_dir=tmp_path, context=_context(tmp_path)),
            backend,
            capture,
        )
    )

    # Then
    content = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    assert report["status"] == "cleaned"
    assert rerun == report
    assert backend.deleted == ["tunnel:tunnel-1"]
    assert parse_cleanup_report(content).pre_install == parse_cleanup_report(content).post_cleanup
