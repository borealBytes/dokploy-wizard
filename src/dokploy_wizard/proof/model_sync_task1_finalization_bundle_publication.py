"""Cleanup-report projection for a ready Task 1 finalization bundle."""

from __future__ import annotations

import json

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_artifacts import require_mapping
from dokploy_wizard.proof.model_sync_state import AbortGuardError
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_finalization import (
    cloudflare_cleanup_payload,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_report import (
    Task1CloudflareCleanupReport,
)


def completed_baseline_payload(content: bytes, report: Task1CloudflareCleanupReport) -> bytes:
    """Replace provisional post-install data with the validated cleanup-report projection."""
    try:
        value = require_mapping(json.loads(content), "Task 1 finalization baseline")
        post_install = require_mapping(
            json.loads(report.post_install.to_bytes()), "post-install Cloudflare snapshot"
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AbortGuardError("Task 1 finalization baseline is invalid") from error
    return (
        proof.canonical_json_bytes(
            {**value, "post_install_cloudflare": post_install, **cloudflare_cleanup_payload(report)}
        )
        + b"\n"
    )
