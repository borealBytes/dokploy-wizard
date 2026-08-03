from __future__ import annotations

from dataclasses import replace

import pytest

from dokploy_wizard.litellm.catalog_observation_types import CatalogModel
from dokploy_wizard.litellm.catalog_retirement import AnomalyRecord
from dokploy_wizard.litellm.catalog_types import ResolvedPrice
from dokploy_wizard.litellm.model_admin import (
    LiteLLMModelAdminConflict,
    build_owned_model_deployment,
)
from dokploy_wizard.litellm.opencode_go_projection import (
    deployment_projection,
    record_projection,
)
from dokploy_wizard.litellm.opencode_go_reconciler import OpenCodeGoDatabaseReconciler
from tests.unit._opencode_go_reconciler_support import (
    MemoryModelAdminApi,
    reconciliation_input,
    record_for,
    sample_catalog_model,
    stale_record,
)


def test_noop_same_catalog_emits_noop_without_mutation() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    api = MemoryModelAdminApi([record_for(deployment)], [], [], [])

    # When
    result = OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert tuple(item.kind for item in result.applied) == ("noop",)
    assert api.mutations == []


def test_pricing_payload_preserves_catalog_provenance() -> None:
    # Given
    model = sample_catalog_model()
    api = MemoryModelAdminApi([], [], [], [])

    # When
    OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    info = api.sent[0].to_api_payload()["model_info"]
    assert isinstance(info, dict)
    assert info["official_pricing_row_sha256"] == "b" * 64
    assert info["dokploy_pricing_input_provenance"] == {
        "selection": "max_both",
        "selected_per_million": 0.3,
        "zero": "not_zero",
        "tiers_sha256": "c" * 64,
    }


def test_zero_to_positive_replaces_pricing_without_stale_values() -> None:
    # Given
    zero = _with_input_price(sample_catalog_model(), 0.0)
    positive = _with_input_price(zero, 0.0000003)
    api = MemoryModelAdminApi(
        [record_for(build_owned_model_deployment(zero, bootstrap_static=False))], [], [], []
    )

    # When
    OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((positive,)))

    # Then
    info = api.sent[0].model_info.to_json()
    assert info["input_cost_per_token"] == 0.0000003
    provenance = info["dokploy_pricing_input_provenance"]
    assert isinstance(provenance, dict)
    assert provenance["zero"] == "not_zero"


def test_positive_to_zero_replaces_pricing_without_stale_values() -> None:
    # Given
    positive = sample_catalog_model()
    zero = _with_input_price(positive, 0.0)
    api = MemoryModelAdminApi(
        [record_for(build_owned_model_deployment(positive, bootstrap_static=False))], [], [], []
    )

    # When
    OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((zero,)))

    # Then
    info = api.sent[0].model_info.to_json()
    assert info["input_cost_per_token"] == 0.0
    provenance = info["dokploy_pricing_input_provenance"]
    assert isinstance(provenance, dict)
    assert provenance["zero"] == "explicit_zero"


def test_extra_server_field_is_ignored_without_mutation() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    api = MemoryModelAdminApi([record_for(deployment, extra_server_field=True)], [], [], [])

    # When
    OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert api.mutations == []


def test_nested_blocked_participates_in_the_owned_projection() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    record = record_for(deployment)
    blocked = replace(record, model_info={**record.model_info, "blocked": True})

    # When
    desired = deployment_projection(deployment)
    actual = record_projection(blocked, deployment)

    # Then
    assert actual.fingerprint != desired.fingerprint


def test_lost_patch_response_accepts_only_exact_reread() -> None:
    # Given
    model = sample_catalog_model()
    static = build_owned_model_deployment(model, bootstrap_static=True)
    api = MemoryModelAdminApi([record_for(static)], [], [], [], lost_update="exact")

    # When
    result = OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert tuple(item.kind for item in result.applied) == ("update",)
    assert api.mutations == [f"update:{static.model_name}"]


def test_eligible_stale_delete_removes_only_proven_owned_alias() -> None:
    # Given
    model = sample_catalog_model()
    stale = sample_catalog_model("retired-model")
    desired = build_owned_model_deployment(model, bootstrap_static=False)
    retired = build_owned_model_deployment(stale, bootstrap_static=False)
    api = MemoryModelAdminApi([record_for(desired), record_for(retired)], [], [], [])
    request = reconciliation_input(
        (model,),
        missing=(stale_record(stale.source_id, (model.source_id,)),),
    )

    # When
    result = OpenCodeGoDatabaseReconciler(api).reconcile(request)

    # Then
    assert tuple(item.kind for item in result.applied) == ("noop", "delete")
    assert api.mutations == [f"delete:{retired.model_id}"]


def test_unauthorized_catalog_state_blocks_without_mutation() -> None:
    # Given
    model = sample_catalog_model()
    api = MemoryModelAdminApi([], [], [], [])

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="not enabled"):
        OpenCodeGoDatabaseReconciler(api).reconcile(
            reconciliation_input((model,), state="disabled")
        )

    # Then
    assert api.mutations == []
    assert api.list_calls == []


def test_missing_pricing_metadata_blocks_without_mutation() -> None:
    # Given
    model = sample_catalog_model()
    incomplete = replace(
        model,
        projection=replace(
            model.projection,
            official_pricing=replace(model.projection.official_pricing, row_sha256=None),
        ),
    )
    api = MemoryModelAdminApi([], [], [], [])

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="pricing metadata"):
        OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((incomplete,)))

    # Then
    assert api.mutations == []


def test_source_mismatch_blocks_without_mutation() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    drifted = replace(
        record_for(deployment),
        model_info={**deployment.model_info.to_json(), "source_id": "other-source"},
    )
    api = MemoryModelAdminApi([drifted], [], [], [])

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="source mismatch"):
        OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert api.mutations == []


def test_anomalous_shrink_blocks_without_mutation() -> None:
    # Given
    model = sample_catalog_model()
    api = MemoryModelAdminApi([], [], [], [])
    anomaly = AnomalyRecord("pending", "a" * 64, 1, 5, 3, None, None)

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="anomalous shrink"):
        OpenCodeGoDatabaseReconciler(api).reconcile(
            reconciliation_input((model,), anomaly=anomaly)
        )

    # Then
    assert api.mutations == []


def test_unowned_alias_blocks_without_mutation() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    record = record_for(deployment)
    unowned = replace(
        record,
        model_info={**record.model_info, "managed_by": "operator"},
    )
    api = MemoryModelAdminApi([unowned], [], [], [])

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="unowned alias"):
        OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert api.mutations == []


def test_lost_response_mismatch_blocks_without_compensation() -> None:
    # Given
    model = sample_catalog_model()
    static = build_owned_model_deployment(model, bootstrap_static=True)
    api = MemoryModelAdminApi([record_for(static)], [], [], [], lost_update="mismatch")

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="lost patch response mismatch"):
        OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert api.mutations == [f"update:{static.model_name}"]


def _with_input_price(model: CatalogModel, value: float) -> CatalogModel:
    tier = model.pricing.tiers[0]
    pricing = replace(
        model.pricing,
        tiers=(
            replace(
                tier,
                input=ResolvedPrice(value=value, provenance="max_both"),
            ),
        ),
        scalar_input_per_token=value,
    )
    return replace(model, pricing=pricing)
