from __future__ import annotations

from pathlib import PurePosixPath
from typing import assert_never

from dokploy_wizard.litellm.catalog_state_types import CatalogState, LkgRecord
from dokploy_wizard.litellm.catalog_state_validation import (
    StateRecordError,
    validate_source_provenance,
)


def validate_catalog_state(state: CatalogState) -> None:
    if state.durable_write_count < 0 or state.durable_write_count % 2 != 0:
        raise StateRecordError("durable write count is invalid")
    _validate_lkg(state.lkg)
    missing_ids = tuple(record.source_id for record in state.missing)
    quarantine_ids = tuple(record.source_id for record in state.quarantine)
    if missing_ids != tuple(sorted(set(missing_ids))):
        raise StateRecordError("missing records are not sorted and unique")
    if quarantine_ids != tuple(sorted(set(quarantine_ids))):
        raise StateRecordError("quarantine records are not sorted and unique")
    if state.last_attempt_at is not None and state.last_success_at is not None:
        if state.last_success_at > state.last_attempt_at:
            raise StateRecordError("state attempt/success time range is invalid")
    if state.observation is None:
        _validate_empty_state(state)
        return
    observation = state.observation
    if tuple(item.source for item in observation.provenance) != (
        "zen",
        "models_dev",
        "official",
    ):
        raise StateRecordError("observation provenance is incomplete")
    for provenance in observation.provenance:
        validate_source_provenance(provenance.source, provenance.commit, provenance.blob)
    if any(item.observed_at > observation.observed_at for item in observation.provenance):
        raise StateRecordError("source provenance is newer than its observation")
    if state.lkg.status == "available":
        if state.lkg.decision_sha256 != observation.decision_sha256:
            raise StateRecordError("LKG and observation decision hashes differ")
        if state.lkg.accepted_ids_sha256 != observation.accepted_ids_sha256:
            raise StateRecordError("LKG and observation accepted-id hashes differ")
    _validate_observation_result(state)


def _validate_lkg(record: LkgRecord) -> None:
    fields = (
        record.generation,
        record.accepted_at,
        record.accepted_ids_sha256,
        record.model_set_sha256,
        record.decision_sha256,
        record.artifact_path,
        record.artifact_sha256,
    )
    match record.status:
        case "empty":
            if fields != (None, None, None, None, None, None, None):
                raise StateRecordError("empty LKG fields are invalid")
        case "available":
            if None in fields:
                raise StateRecordError("available LKG fields are incomplete")
            assert record.generation is not None
            assert record.model_set_sha256 is not None
            assert record.artifact_path is not None
            if record.artifact_sha256 != record.model_set_sha256:
                raise StateRecordError("LKG artifact and model-set hashes differ")
            path = PurePosixPath(record.artifact_path)
            expected = f"{record.generation}-{record.model_set_sha256}.json"
            if not path.is_absolute() or path.name != expected or path.parent.name != "generations":
                raise StateRecordError("LKG artifact path is invalid")
        case unreachable:
            assert_never(unreachable)


def _validate_empty_state(state: CatalogState) -> None:
    if (
        state.last_attempt_at is not None
        or state.last_success_at is not None
        or state.last_input_sha256 is not None
        or state.last_output_sha256 is not None
        or state.durable_write_count != 0
        or state.lkg.status != "empty"
        or state.anomaly.state != "none"
        or state.missing
        or state.quarantine
        or state.last_result is not None
    ):
        raise StateRecordError("state without observation has durable fields")


def _validate_observation_result(state: CatalogState) -> None:
    observation = state.observation
    assert observation is not None
    if observation.status in {"accepted", "accepted_with_quarantine"}:
        if not observation.complete or not observation.accepted_ids:
            raise StateRecordError("accepted observation is incomplete")
    if observation.status == "accepted" and state.quarantine:
        raise StateRecordError("accepted observation has quarantine records")
    if observation.status == "accepted_with_quarantine" and not state.quarantine:
        raise StateRecordError("quarantined observation lacks quarantine records")
    if not set(record.source_id for record in state.quarantine).issubset(
        observation.source_ids
    ):
        raise StateRecordError("quarantine record is absent from source ids")
    result = state.last_result
    if result is None:
        return
    if state.last_attempt_at != result.ended_at:
        raise StateRecordError("last result does not bind last attempt")
    if result.input_sha256 != state.last_input_sha256:
        raise StateRecordError("last result does not bind input hash")
    if result.output_sha256 != state.last_output_sha256:
        raise StateRecordError("last result does not bind output hash")
    match result.status:
        case "success":
            if observation.status != "accepted" or state.quarantine:
                raise StateRecordError("success result conflicts with quarantine")
        case "success_with_quarantine":
            if observation.status != "accepted_with_quarantine" or not state.quarantine:
                raise StateRecordError("quarantined success result is inconsistent")
        case "no_change" | "quarantined" | "failed":
            return
        case unreachable:
            assert_never(unreachable)
