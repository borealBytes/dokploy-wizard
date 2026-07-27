from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from dokploy_wizard.litellm.catalog_json import canonical_json_bytes
from dokploy_wizard.litellm.catalog_missing import MissingRecord
from dokploy_wizard.litellm.catalog_retirement import AnomalyRecord
from dokploy_wizard.litellm.catalog_sources import OFFICIAL_SOURCE
from dokploy_wizard.litellm.catalog_state_types import (
    CatalogState,
    LastResult,
    LkgRecord,
    StateObservation,
    StateQuarantineRecord,
)
from dokploy_wizard.litellm.catalog_types import SourceProvenance


def complete_state() -> CatalogState:
    observed_at = datetime(2026, 7, 1, tzinfo=UTC)
    source_ids = ("model-a", "unknown-model")
    source_ids_sha = hashlib.sha256(canonical_json_bytes(list(source_ids))).hexdigest()
    accepted_ids_sha = hashlib.sha256(canonical_json_bytes(["model-a"])).hexdigest()
    raw_sha = hashlib.sha256(b"raw").hexdigest()
    projected_sha = hashlib.sha256(b"projected").hexdigest()
    decision_sha = hashlib.sha256(b"decision").hexdigest()
    model_sha = hashlib.sha256(b"model").hexdigest()
    provenance = SourceProvenance(
        "zen",
        "https://example.invalid/models",
        "application/json",
        3,
        raw_sha,
        projected_sha,
        None,
        None,
        observed_at,
    )
    models_provenance = provenance.__class__(
        "models_dev",
        "https://models.example.invalid/api.json",
        "application/json",
        3,
        raw_sha,
        projected_sha,
        None,
        None,
        observed_at,
    )
    official_provenance = provenance.__class__(
        "official",
        "https://official.example.invalid/pinned",
        "text/plain",
        3,
        raw_sha,
        projected_sha,
        OFFICIAL_SOURCE.commit,
        OFFICIAL_SOURCE.blob,
        observed_at,
    )
    return CatalogState(
        1,
        2,
        "opencode-go",
        "enabled",
        observed_at,
        observed_at,
        raw_sha,
        model_sha,
        2,
        StateObservation(
            "accepted_with_quarantine",
            observed_at,
            True,
            source_ids,
            source_ids_sha,
            2,
            ("model-a",),
            accepted_ids_sha,
            1,
            decision_sha,
            (provenance, models_provenance, official_provenance),
        ),
        LkgRecord(
            "available",
            1,
            observed_at,
            accepted_ids_sha,
            model_sha,
            decision_sha,
            f"/state/generations/1-{model_sha}.json",
            model_sha,
        ),
        AnomalyRecord("none", None, None, None, None, None, None),
        (
            MissingRecord(
                "old-model",
                "pending_absence",
                observed_at,
                observed_at,
                None,
                None,
                source_ids_sha,
            ),
        ),
        (
            StateQuarantineRecord(
                "unknown-model",
                "missing_official_transport",
                observed_at,
                observed_at,
                raw_sha,
                None,
            ),
        ),
        LastResult(
            "success_with_quarantine",
            "accepted",
            observed_at,
            observed_at,
            raw_sha,
            model_sha,
            2,
        ),
    )
