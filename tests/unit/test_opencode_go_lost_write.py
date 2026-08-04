from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from dokploy_wizard.litellm.model_admin_payload import build_owned_model_deployment
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminWriteAmbiguity,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
    ModelUuid,
)
from dokploy_wizard.litellm.opencode_go_plan import OpenCodeGoReconciliationInput
from dokploy_wizard.litellm.opencode_go_reconciler import OpenCodeGoDatabaseReconciler
from tests.unit._opencode_go_reconciler_support import (
    reconciliation_input,
    record_for,
    sample_catalog_model,
)


@dataclass(frozen=True, slots=True)
class _AmbiguousCreateApi:
    recovered: tuple[LiteLLMModelRecord, ...]
    list_calls: list[int] = field(default_factory=list)

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        self.list_calls.append(len(self.list_calls))
        return () if len(self.list_calls) <= 2 else self.recovered

    def create_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise LiteLLMModelAdminWriteAmbiguity("ambiguous create")

    def update_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("create recovery must not update")

    def delete_model(self, _: ModelUuid) -> None:
        raise AssertionError("create recovery must not delete")


@dataclass(frozen=True, slots=True)
class _TransformedCreateResponseApi:
    stored: LiteLLMModelRecord
    response: LiteLLMModelRecord
    list_calls: list[int] = field(default_factory=list)

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        self.list_calls.append(len(self.list_calls))
        return () if len(self.list_calls) <= 2 else (self.stored,)

    def create_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        return self.response

    def update_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("create confirmation must not update")

    def delete_model(self, _: ModelUuid) -> None:
        raise AssertionError("create confirmation must not delete")


def test_create_confirms_transformed_response_from_database_inventory() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    record = record_for(deployment)
    stored = replace(
        record,
        litellm_params=replace(record.litellm_params, api_key=None),
    )
    response = replace(
        record,
        litellm_params=replace(record.litellm_params, api_key="encrypted"),
    )
    api = _TransformedCreateResponseApi(stored, response)

    # When
    result = OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert result.applied[0].record == stored
    assert api.list_calls == [0, 1, 2]


@pytest.mark.parametrize(
    ("recovered_count", "expected"),
    (
        (0, "lost create response left no owned alias"),
        (2, "lost create response left multiple owned aliases"),
    ),
)
def test_ambiguous_create_classifies_recovered_alias_count(
    recovered_count: int,
    expected: str,
) -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    record = record_for(deployment)
    recovered = (
        ()
        if recovered_count == 0
        else (record, replace(record, model_id=ModelUuid("00000000-0000-4000-8000-000000000000")))
    )
    api = _AmbiguousCreateApi(recovered)
    request: OpenCodeGoReconciliationInput = reconciliation_input((model,))

    # When / Then
    with pytest.raises(LiteLLMModelAdminConflict, match=expected):
        OpenCodeGoDatabaseReconciler(api).reconcile(request)
