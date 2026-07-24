from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dokploy_wizard.proof import BaselineAttestation, EnvReceipt, build_result
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
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
from dokploy_wizard.proof.model_sync_task1_context import derive_task1_proof_context
from dokploy_wizard.proof.model_sync_task1_evidence import restore_task1_proof_source
from dokploy_wizard.proof.model_sync_task1_materialization import (
    materialize_task1_external_files,
)


def active_receipt(tmp_path: Path) -> EnvReceipt:
    source_path = tmp_path / ".install-min.env"
    source_bytes = (
        b"AI_DEFAULT_MODEL=example/model\nAI_DEFAULT_PROVIDER=openrouter\n"
        b"PACKS=coder\nROOT_DOMAIN=example.test\n"
    )
    source_path.write_bytes(source_bytes)
    source_path.chmod(0o600)
    prepared = derive_task1_proof_context(
        source_values={
            "AI_DEFAULT_MODEL": "example/model",
            "AI_DEFAULT_PROVIDER": "openrouter",
            "PACKS": "coder",
            "ROOT_DOMAIN": "example.test",
        },
        source_bytes=source_bytes,
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(prepared.materialization)
    backup_path = tmp_path / "backup.env"
    backup_path.write_bytes(source_bytes)
    backup_path.chmod(0o600)
    context_evidence = restore_task1_proof_source(
        prepared=prepared,
        backup_path=backup_path,
    )
    return EnvReceipt(
        str(source_path.resolve()),
        str(backup_path.resolve()),
        prepared.context.source_env_sha256,
        prepared.context.source_env_sha256,
        0o600,
        context_evidence,
    )


def snapshot(context_sha256: str, *, tunnel: bool) -> CloudflareSnapshotV1:
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
    if tunnel:
        resources += (
            CloudflareSnapshotResourceV1(
                "tunnel",
                "c" * 64,
                {
                    "configuration_availability": "read",
                    "configuration_sha256": "d" * 64,
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
            len(tuple(item for item in resources if item.kind == kind)),
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


def snapshot_with_additions(
    context_sha256: str, additions: tuple[CloudflareSnapshotResourceV1, ...]
) -> CloudflareSnapshotV1:
    base = snapshot(context_sha256, tunnel=False)
    resources = (*base.resources, *additions)
    collections = tuple(
        CloudflareSnapshotCollectionV1(
            kind,
            0 if kind == "access_policy" else 1,
            100,
            sum(item.kind == kind for item in resources),
        )
        for kind in KINDS
    )
    return CloudflareSnapshotV1.create(
        context_sha256=base.context_sha256,
        account_id_sha256=base.account_id_sha256,
        zone_id_sha256=base.zone_id_sha256,
        collections=collections,
        resources=resources,
        otp_provider_sha256=base.otp_provider_sha256,
    )


def cleanup_report(context_sha256: str) -> Task1CloudflareCleanupReport:
    pre_install = snapshot(context_sha256, tunnel=False)
    post_install = snapshot(context_sha256, tunnel=True)
    receipt_bytes = (
        json.dumps(
            {
                "context_sha256": context_sha256,
                "operations_sha256": "d" * 64,
                "status": "cleaned",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    return Task1CloudflareCleanupReport(
        pre_install,
        post_install,
        pre_install,
        receipt_bytes,
        "d" * 64,
        post_install.snapshot_sha256,
    )


def legacy_attestation() -> BaselineAttestation:
    artifact_dir = Path("/tmp/task1-v2-golden/artifacts")
    receipt = EnvReceipt(
        "/tmp/task1-v2-golden/install.env",
        "/tmp/task1-v2-golden/install.env.backup",
        "a" * 64,
        "b" * 64,
        0o600,
    )
    result = build_result(
        {
            "schema_version": 2,
            "source_base_commit": "a" * 40,
            "proof_commit": "b" * 40,
            "host_identity_mode": "distinct",
            "coder_image_digest": "ghcr.io/coder/coder@sha256:" + "1" * 64,
            "litellm_image_digest": "ghcr.io/berriai/litellm@sha256:" + "2" * 64,
            "shared_core_image_digests": {
                "pgvector": "pgvector/pgvector@sha256:" + "3" * 64,
                "redis": "redis@sha256:" + "4" * 64,
                "postfix": "postfix@sha256:" + "5" * 64,
                "litellm": "ghcr.io/berriai/litellm@sha256:" + "2" * 64,
            },
            "env_original_sha256": receipt.original_sha256,
            "env_proof_sha256": receipt.proof_sha256,
            "env_mode": receipt.mode,
            "external_backup_path": receipt.backup_path,
            "abort_guard_path": "/tmp/task1-v2-golden/abort-guard.json",
            "abort_guard_sha256": "6" * 64,
            "host_a_preflight_sha256": "d" * 64,
            "host_b_preflight_sha256": "e" * 64,
            "host_identities_distinct": True,
            "host_architectures_equal": True,
            "single_host_lifecycle_path": None,
            "single_host_lifecycle_sha256": None,
            "temporal_clean_epoch_evidence": False,
            "baseline_sha256": "f" * 64,
            "protected_artifacts_before_path": str(artifact_dir / "protected-artifacts-before.txt"),
            "protected_artifacts_before_sha256": "7" * 64,
            "coder_secret_inventory_sha256": "8" * 64,
            "legacy_workspace_managed_fingerprints_sha256": "9" * 64,
            "preexisting_cloudflare_sha256": "a" * 64,
            "post_install_cloudflare_sha256": "b" * 64,
        }
    )
    return BaselineAttestation(
        "c" * 64,
        "/tmp/task1-v2-golden/abort-guard.json",
        str(artifact_dir),
        str(artifact_dir / "result.json"),
        receipt,
        "distinct",
        "b" * 64,
        {
            "baseline.json": "f" * 64,
            "host-a-preflight.json": "d" * 64,
            "host-b-preflight.json": "e" * 64,
            "protected-artifacts-before.txt": "7" * 64,
        },
        {key: value for key, value in result.items() if key != "abort_guard_sha256"},
    )
