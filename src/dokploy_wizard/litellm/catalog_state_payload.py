from __future__ import annotations

from datetime import datetime

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.catalog_missing import MissingRecord
from dokploy_wizard.litellm.catalog_retirement import AnomalyRecord
from dokploy_wizard.litellm.catalog_state_types import (
    CatalogState,
    LastResult,
    LkgRecord,
    StateObservation,
    StateQuarantineRecord,
)
from dokploy_wizard.litellm.catalog_types import SourceProvenance


def catalog_state_payload(state: CatalogState) -> JsonValue:
    return {
        "anomaly": _anomaly_payload(state.anomaly),
        "catalog_id": state.catalog_id,
        "durable_write_count": state.durable_write_count,
        "last_attempt_at": timestamp_payload(state.last_attempt_at),
        "last_input_sha256": state.last_input_sha256,
        "last_output_sha256": state.last_output_sha256,
        "last_result": None if state.last_result is None else _result_payload(state.last_result),
        "last_success_at": timestamp_payload(state.last_success_at),
        "lkg": _lkg_payload(state.lkg),
        "missing": [_missing_payload(record) for record in state.missing],
        "observation": (
            None if state.observation is None else _observation_payload(state.observation)
        ),
        "quarantine": [_quarantine_payload(record) for record in state.quarantine],
        "schema_version": state.schema_version,
        "state": state.state,
        "sync_contract_version": state.sync_contract_version,
    }


def _observation_payload(value: StateObservation) -> JsonValue:
    return {
        "accepted_ids": list(value.accepted_ids),
        "accepted_ids_count": value.accepted_ids_count,
        "accepted_ids_sha256": value.accepted_ids_sha256,
        "complete": value.complete,
        "decision_sha256": value.decision_sha256,
        "observed_at": timestamp_payload(value.observed_at),
        "provenance": [_provenance_payload(item) for item in value.provenance],
        "source_ids": list(value.source_ids),
        "source_ids_count": value.source_ids_count,
        "source_ids_sha256": value.source_ids_sha256,
        "status": value.status,
    }


def _provenance_payload(value: SourceProvenance) -> JsonValue:
    return {
        "blob": value.blob,
        "byte_count": value.byte_count,
        "commit": value.commit,
        "content_type": value.content_type,
        "projected_sha256": value.projected_sha256,
        "raw_sha256": value.raw_sha256,
        "observed_at": timestamp_payload(value.observed_at),
        "source": value.source,
        "url": value.url,
    }


def _lkg_payload(value: LkgRecord) -> JsonValue:
    return {
        "accepted_at": timestamp_payload(value.accepted_at),
        "accepted_ids_sha256": value.accepted_ids_sha256,
        "artifact_path": value.artifact_path,
        "artifact_sha256": value.artifact_sha256,
        "decision_sha256": value.decision_sha256,
        "generation": value.generation,
        "model_set_sha256": value.model_set_sha256,
        "status": value.status,
    }


def _anomaly_payload(value: AnomalyRecord) -> JsonValue:
    return {
        "candidate_count": value.candidate_count,
        "candidate_ids_sha256": value.candidate_ids_sha256,
        "confirmed_at": timestamp_payload(value.confirmed_at),
        "first_seen_at": timestamp_payload(value.first_seen_at),
        "previous_count": value.previous_count,
        "state": value.state,
        "threshold": value.threshold,
    }


def _missing_payload(value: MissingRecord) -> JsonValue:
    return {
        "delete_not_before": timestamp_payload(value.delete_not_before),
        "first_missing_at": timestamp_payload(value.first_missing_at),
        "last_present_at": timestamp_payload(value.last_present_at),
        "observation_sha256": value.observation_sha256,
        "second_missing_at": timestamp_payload(value.second_missing_at),
        "source_id": value.source_id,
        "state": value.state,
    }


def _quarantine_payload(value: StateQuarantineRecord) -> JsonValue:
    return {
        "first_seen_at": timestamp_payload(value.first_seen_at),
        "last_seen_at": timestamp_payload(value.last_seen_at),
        "preserved_visible_row_sha256": value.preserved_visible_row_sha256,
        "reason": value.reason,
        "source_id": value.source_id,
        "source_sha256": value.source_sha256,
    }


def _result_payload(value: LastResult) -> JsonValue:
    return {
        "durable_write_delta": value.durable_write_delta,
        "ended_at": timestamp_payload(value.ended_at),
        "input_sha256": value.input_sha256,
        "output_sha256": value.output_sha256,
        "reason": value.reason,
        "started_at": timestamp_payload(value.started_at),
        "status": value.status,
    }


def timestamp_payload(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")
