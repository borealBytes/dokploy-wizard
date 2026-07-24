from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_evidence import (
    CloudflareSnapshotEvidenceError,
    load_or_capture_snapshot,
    validate_cleanup_evidence,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotCollectionV1,
    CloudflareSnapshotResourceV1,
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema_types import (
    KINDS,
    OTP_NAME_SHA256,
)


def _snapshot(context_sha256: str, *, tunnel: bool = False) -> CloudflareSnapshotV1:
    provider = CloudflareSnapshotResourceV1(
        "identity_provider",
        "a" * 64,
        {
            "name_sha256": OTP_NAME_SHA256,
            "provider_type": "onetimepin",
            "spec_sha256": "b" * 64,
        },
    )
    resources: tuple[CloudflareSnapshotResourceV1, ...] = (provider,)
    collections: tuple[CloudflareSnapshotCollectionV1, ...] = tuple(
        CloudflareSnapshotCollectionV1(
            kind,
            0 if kind == "access_policy" else 1,
            100,
            1 if kind == "identity_provider" else 0,
        )
        for kind in KINDS
    )
    if tunnel:
        resources += (
            CloudflareSnapshotResourceV1(
                "tunnel",
                "c" * 64,
                {
                    "configuration_sha256": "d" * 64,
                    "configuration_availability": "read",
                    "config_src": "cloudflare",
                    "name_sha256": "e" * 64,
                    "spec_sha256": "f" * 64,
                    "status": "unknown",
                },
            ),
        )
        collections = tuple(
            CloudflareSnapshotCollectionV1(
                kind,
                0 if kind == "access_policy" else 1,
                100,
                1 if kind in {"identity_provider", "tunnel"} else 0,
            )
            for kind in KINDS
        )
    return CloudflareSnapshotV1.create(
        context_sha256=context_sha256,
        account_id_sha256=hashlib.sha256(b"account-a").hexdigest(),
        zone_id_sha256=hashlib.sha256(b"zone-a").hexdigest(),
        collections=collections,
        resources=resources,
        otp_provider_sha256=provider.semantic_sha256,
    )


def test_preinstall_artifact_is_exactly_reused_when_already_published(tmp_path: Path) -> None:
    # Given: a snapshot capture callback and a context-bound mode-0600 artifact path.
    calls = 0
    expected = _snapshot("3" * 64)

    def capture() -> CloudflareSnapshotV1:
        nonlocal calls
        calls += 1
        return expected

    path = tmp_path / "pre-install-cloudflare.snapshot.json"

    # When: restart asks for the same preimage after publication.
    scope = CloudflareSnapshotScope("3" * 64, "account-a", "zone-a")
    first = load_or_capture_snapshot(path, scope, capture)
    second = load_or_capture_snapshot(path, scope, capture)

    # Then: capture ran once and persisted bytes remain authoritative.
    assert first == second == expected
    assert calls == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_cleanup_evidence_rejects_snapshot_or_receipt_drift_when_validated() -> None:
    # Given: identical pre/cleanup snapshots and a redacted cleanup receipt.
    pre = _snapshot("3" * 64)
    receipt = (
        json.dumps(
            {
                "context_sha256": "3" * 64,
                "operations_sha256": "4" * 64,
                "status": "cleaned",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )

    # When / Then: any post-cleanup snapshot change is rejected.
    with pytest.raises(CloudflareSnapshotEvidenceError):
        validate_cleanup_evidence(
            pre,
            _snapshot("3" * 64, tunnel=True),
            receipt,
            "4" * 64,
        )
