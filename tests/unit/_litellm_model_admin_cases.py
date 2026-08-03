from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import request

import pytest

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.model_admin import (
    LiteLLMInventoryRoutingParams,
    LiteLLMModelAdminApi,
    LiteLLMModelAdminClient,
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelAdminReconciler,
    LiteLLMModelAdminWriteAmbiguity,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
    build_owned_model_deployment,
)

from ._litellm_model_admin_support import catalog_model


def _stored_row(
    deployment: LiteLLMModelDeployment, *, model_id: str | None = None
) -> dict[str, JsonValue]:
    payload = deployment.to_api_payload()
    model_info = payload["model_info"]
    assert isinstance(model_info, dict)
    stored_info = dict(model_info)
    if model_id is not None:
        stored_info["id"] = model_id
    return {
        "model_id": stored_info["id"],
        "model_name": payload["model_name"],
        "litellm_params": payload["litellm_params"],
        "model_info": stored_info,
    }


def test_deterministic_uuid_and_full_owned_projection() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=True)

    payload = deployment.to_api_payload()
    model_info = payload["model_info"]
    assert isinstance(model_info, dict)

    assert deployment.model_id == "f1d37191-9f00-5dbe-8c31-419b86525e28"
    assert payload["model_name"] == "opencode-go/minimax-m2.7"
    assert payload["litellm_params"] == {
        "model": "openai/minimax-m2.7",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "os.environ/LITELLM_OPENCODE_GO_API_KEY",
    }
    assert model_info["bootstrap_static"] is True
    assert model_info["cache_read_input_token_cost"] == 0.00000006
    assert model_info["cache_creation_input_token_cost"] == 0.000000375
    assert model_info["dokploy_source_cache_read_per_million"] == 0.06
    assert model_info["dokploy_scalar_cache_write_per_million"] == 0.375
    assert model_info["dokploy_pricing_cache_read_provenance"] == {
        "selection": "input_fallback",
        "selected_per_million": 0.06,
        "zero": "not_zero",
        "tiers_sha256": "c" * 64,
    }


def test_create_uses_exact_admin_transport_and_unmasked_three_key_payload() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    recorded: list[tuple[str, str, dict[str, str], JsonValue]] = []

    def request_fn(raw_request: request.Request) -> JsonValue:
        raw_body = raw_request.data
        assert isinstance(raw_body, bytes)
        recorded.append(
            (
                raw_request.get_method(),
                raw_request.full_url,
                {name.lower(): value for name, value in raw_request.header_items()},
                json.loads(raw_body.decode("utf-8")),
            )
        )
        return _stored_row(deployment)

    stored = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=request_fn,
    ).create_model(deployment)

    assert stored.model_id == deployment.model_id
    assert recorded == [
        (
            "POST",
            "http://litellm.internal/model/new",
            {
                "accept": "application/json",
                "authorization": "Bearer test-master-key",
                "content-type": "application/json",
            },
            deployment.to_api_payload(),
        )
    ]


def test_path_body_id_mismatch_is_rejected() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)

    def request_fn(_: request.Request) -> JsonValue:
        return _stored_row(deployment, model_id="0c21a2b8-6e0e-4c20-b59d-62c5b62009f4")

    client = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=request_fn,
    )

    with pytest.raises(LiteLLMModelAdminError, match="path/body id mismatch"):
        client.update_model(deployment)


def test_legacy_post_update_rejected_by_exact_patch_route() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    recorded: list[tuple[str, str]] = []

    def request_fn(raw_request: request.Request) -> JsonValue:
        recorded.append((raw_request.get_method(), raw_request.full_url))
        return _stored_row(deployment)

    LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=request_fn,
    ).update_model(deployment)

    assert recorded == [
        (
            "PATCH",
            "http://litellm.internal/model/f1d37191-9f00-5dbe-8c31-419b86525e28/update",
        )
    ]


@dataclass(frozen=True, slots=True)
class _ForeignAliasApi(LiteLLMModelAdminApi):
    record: LiteLLMModelRecord

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        return (self.record,)

    def create_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("foreign alias must not be created")

    def update_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("foreign alias must not be patched")

    def delete_model(self, _: str) -> None:
        raise AssertionError("foreign alias must not be deleted")


def test_foreign_alias_conflict_is_rejected_without_mutation() -> None:
    desired = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    foreign = LiteLLMModelRecord(
        model_id=desired.model_id,
        model_name=desired.model_name,
        litellm_params=LiteLLMInventoryRoutingParams.from_owned(desired.litellm_params),
        model_info={**desired.model_info.to_json(), "managed_by": "operator"},
        server_model_info_extras={},
    )

    with pytest.raises(LiteLLMModelAdminConflict, match="foreign alias conflict"):
        LiteLLMModelAdminReconciler(_ForeignAliasApi(foreign)).reconcile(
            (desired,),
        )


@dataclass(frozen=True, slots=True)
class _AmbiguousUpdateApi(LiteLLMModelAdminApi):
    record: LiteLLMModelRecord

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        return (self.record,)

    def create_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("create must not occur")

    def update_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise LiteLLMModelAdminWriteAmbiguity("ambiguous update")

    def delete_model(self, _: str) -> None:
        raise AssertionError("delete must not occur")


def test_ambiguous_timeout_mismatch_fails_closed() -> None:
    desired = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    mismatched = LiteLLMModelRecord(
        model_id=desired.model_id,
        model_name=desired.model_name,
        litellm_params=LiteLLMInventoryRoutingParams.from_owned(desired.litellm_params),
        model_info={**desired.model_info.to_json(), "dokploy_spec_sha256": "0" * 64},
        server_model_info_extras={},
    )

    with pytest.raises(LiteLLMModelAdminConflict, match="ambiguous update"):
        LiteLLMModelAdminReconciler(_AmbiguousUpdateApi(mismatched)).reconcile((desired,))
