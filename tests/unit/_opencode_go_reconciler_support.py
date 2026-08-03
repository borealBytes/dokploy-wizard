from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from dokploy_wizard.litellm.catalog_json import (
    JsonValue,
    canonical_json_bytes,
    sha256_bytes,
)
from dokploy_wizard.litellm.catalog_missing import MissingRecord
from dokploy_wizard.litellm.catalog_observation_types import CatalogModel
from dokploy_wizard.litellm.catalog_retirement import AnomalyRecord
from dokploy_wizard.litellm.catalog_state_types import (
    CatalogState,
    LkgRecord,
    StateObservation,
)
from dokploy_wizard.litellm.model_admin import (
    LiteLLMInventoryRoutingParams,
    LiteLLMModelAdminApi,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
)
from dokploy_wizard.litellm.opencode_go_plan import OpenCodeGoReconciliationInput
from tests.unit._litellm_model_admin_support import catalog_model

_OBSERVED_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_DUMMY_SHA256 = "f" * 64
LostUpdateMode = Literal["none", "exact", "mismatch"]


def reconciliation_input(
    models: tuple[CatalogModel, ...],
    *,
    missing: tuple[MissingRecord, ...] = (),
    anomaly: AnomalyRecord | None = None,
    complete: bool = True,
    state: Literal["enabled", "disabled", "blocked"] = "enabled",
) -> OpenCodeGoReconciliationInput:
    source_ids = tuple(model.source_id for model in models)
    source_sha = sha256_bytes(canonical_json_bytes(list(source_ids)))
    observation = StateObservation(
        status="accepted",
        observed_at=_OBSERVED_AT,
        complete=complete,
        source_ids=source_ids,
        source_ids_sha256=source_sha,
        source_ids_count=len(source_ids),
        accepted_ids=source_ids,
        accepted_ids_sha256=source_sha,
        accepted_ids_count=len(source_ids),
        decision_sha256=_DUMMY_SHA256,
        provenance=(),
    )
    catalog_state = CatalogState(
        schema_version=1,
        sync_contract_version=2,
        catalog_id="opencode-go",
        state=state,
        last_attempt_at=None,
        last_success_at=None,
        last_input_sha256=None,
        last_output_sha256=None,
        durable_write_count=0,
        observation=observation,
        lkg=LkgRecord.empty(),
        anomaly=AnomalyRecord.empty() if anomaly is None else anomaly,
        missing=missing,
        quarantine=(),
        last_result=None,
    )
    return OpenCodeGoReconciliationInput(
        models=models,
        catalog_state=catalog_state,
        bootstrap_static=False,
    )


def stale_record(source_id: str, visible_source_ids: tuple[str, ...]) -> MissingRecord:
    return MissingRecord(
        source_id=source_id,
        state="eligible_for_delete",
        last_present_at=_OBSERVED_AT,
        first_missing_at=_OBSERVED_AT,
        second_missing_at=_OBSERVED_AT,
        delete_not_before=_OBSERVED_AT,
        observation_sha256=sha256_bytes(canonical_json_bytes(list(visible_source_ids))),
    )


def record_for(
    deployment: LiteLLMModelDeployment, *, extra_server_field: bool = False
) -> LiteLLMModelRecord:
    model_info = deployment.model_info.to_json()
    extras: dict[str, JsonValue] = {}
    if extra_server_field:
        model_info["server_note"] = "preserve"
        extras["server_note"] = "preserve"
    return LiteLLMModelRecord(
        model_id=deployment.model_id,
        model_name=deployment.model_name,
        litellm_params=LiteLLMInventoryRoutingParams.from_owned(deployment.litellm_params),
        model_info=model_info,
        server_model_info_extras=extras,
    )


@dataclass(frozen=True, slots=True)
class MemoryModelAdminApi(LiteLLMModelAdminApi):
    rows: list[LiteLLMModelRecord]
    mutations: list[str]
    sent: list[LiteLLMModelDeployment]
    list_calls: list[int]
    lost_update: LostUpdateMode = "none"
    delete_returns_400: bool = False
    delete_preserves_row: bool = False

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        self.list_calls.append(len(self.list_calls))
        return tuple(self.rows)

    def create_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        self.mutations.append(f"create:{deployment.model_name}")
        self.sent.append(deployment)
        record = record_for(deployment)
        self.rows.append(record)
        return record

    def update_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        self.mutations.append(f"update:{deployment.model_name}")
        self.sent.append(deployment)
        record = record_for(deployment)
        if self.lost_update == "mismatch":
            model_info = dict(record.model_info)
            model_info["dokploy_spec_sha256"] = "0" * 64
            record = replace(record, model_info=model_info)
        self.rows[:] = [
            record if existing.model_id == deployment.model_id else existing
            for existing in self.rows
        ]
        if self.lost_update != "none":
            raise LiteLLMModelAdminWriteAmbiguity("ambiguous update")
        return record

    def delete_model(self, model_id: str) -> None:
        self.mutations.append(f"delete:{model_id}")
        if not self.delete_preserves_row:
            self.rows[:] = [record for record in self.rows if record.model_id != model_id]
        if self.delete_returns_400:
            raise LiteLLMModelAdminError("LiteLLM model admin request failed with status 400")


def sample_catalog_model(source_id: str = "minimax-m2.7") -> CatalogModel:
    return catalog_model(source_id=source_id)
