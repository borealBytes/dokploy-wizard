from __future__ import annotations

import hashlib
from pathlib import Path

from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    dns_fingerprint,
    snapshot_value_hash,
    tunnel_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalCleanupState,
    JournalOperationKind,
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_delta import (
    validate_install_delta,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotResourceV1,
)
from tests.unit._model_sync_task1_cloudflare_journal_cleanup_support import CleanupBackend
from tests.unit._model_sync_task1_cloudflare_journal_common import _context, _intent
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    snapshot,
    snapshot_with_additions,
)


def test_install_delta_accepts_active_journal_after_partial_cleanup_checkpoint(
    tmp_path: Path,
) -> None:
    # Given
    context = _context(tmp_path)
    context_sha256 = hashlib.sha256(context.to_bytes()).hexdigest()
    backend = CleanupBackend()
    tunnel = backend.tunnels["tunnel-1"]
    record = backend.records["dns-1"]
    journal = Task1CloudflareJournal.open(state_dir=tmp_path, context=context)
    tunnel_operation = journal.record_intent(
        _intent(JournalOperationKind.TUNNEL, {"account_id": "account", "name": tunnel.name})
    )
    journal.checkpoint_created(
        tunnel_operation,
        response_id=tunnel.tunnel_id,
        post_fingerprint_sha256=tunnel_fingerprint(tunnel),
    )
    dns_operation = journal.record_intent(
        _intent(
            JournalOperationKind.DNS_RECORD,
            {
                "account_id": "account",
                "zone_id": "zone",
                "hostname": record.name,
            },
        )
    )
    created_dns = journal.checkpoint_created(
        dns_operation,
        response_id=record.record_id,
        post_fingerprint_sha256=dns_fingerprint(record),
    )
    journal.checkpoint_cleaned(created_dns, JournalCleanupState.VERIFIED_ABSENT)
    post_install = snapshot_with_additions(
        context_sha256,
        (
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
            ),
            CloudflareSnapshotResourceV1(
                "dns_record",
                snapshot_value_hash(record.record_id),
                {
                    "content_sha256": snapshot_value_hash(record.content),
                    "name_sha256": snapshot_value_hash(record.name),
                    "proxied": record.proxied,
                    "record_type": record.record_type,
                    "spec_sha256": dns_fingerprint(record),
                },
            ),
        ),
    )

    # When / Then
    validate_install_delta(snapshot(context_sha256, tunnel=False), post_install, journal.document())
