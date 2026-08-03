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
    LiteLLMModelAdminError,
    LiteLLMModelAdminReconciler,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
    build_owned_model_deployment,
)

from ._litellm_model_admin_support import catalog_model


def _inventory_row(
    deployment: LiteLLMModelDeployment, *, api_key: str = "os.environ/LITELLM_OPENCODE_GO_API_KEY"
) -> dict[str, JsonValue]:
    params = deployment.litellm_params.to_json()
    params["api_key"] = api_key
    return {
        "model_name": deployment.model_name,
        "litellm_params": params,
        "model_info": deployment.model_info.to_json(),
    }


def test_masked_parameter_is_rejected_from_inventory() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)

    def request_fn(_: request.Request) -> JsonValue:
        return {"data": [_inventory_row(deployment, api_key="********")]}

    client = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=request_fn,
    )

    with pytest.raises(LiteLLMModelAdminError, match="masked"):
        client.list_models()


def test_inventory_accepts_removed_api_key_without_reusing_it() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)

    def request_fn(_: request.Request) -> JsonValue:
        row = _inventory_row(deployment)
        params = row["litellm_params"]
        assert isinstance(params, dict)
        del params["api_key"]
        return {"data": [row]}

    records = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=request_fn,
    ).list_models()

    assert records[0].litellm_params.api_key is None


def test_top_level_blocked_rejected_from_inventory() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)

    def request_fn(_: request.Request) -> JsonValue:
        row = _inventory_row(deployment)
        row["blocked"] = False
        return {"data": [row]}

    client = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=request_fn,
    )

    with pytest.raises(LiteLLMModelAdminError, match="nested in model_info"):
        client.list_models()


def test_owned_delete_uses_exact_route_body_and_success_message() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    recorded: list[tuple[str, str, dict[str, JsonValue]]] = []

    def request_fn(raw_request: request.Request) -> JsonValue:
        raw_body = raw_request.data
        assert isinstance(raw_body, bytes)
        recorded.append(
            (
                raw_request.get_method(),
                raw_request.full_url,
                json.loads(raw_body.decode("utf-8")),
            )
        )
        return {"message": f"Model: {deployment.model_id} deleted successfully"}

    LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=request_fn,
    ).delete_model(deployment.model_id)

    assert recorded == [
        ("POST", "http://litellm.internal/model/delete", {"id": deployment.model_id})
    ]


@dataclass(frozen=True, slots=True)
class _DeleteApi(LiteLLMModelAdminApi):
    record: LiteLLMModelRecord
    deleted: list[str]

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        return (self.record,)

    def create_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("create must not occur")

    def update_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("update must not occur")

    def delete_model(self, model_id: str) -> None:
        self.deleted.append(model_id)


def test_owned_delete_removes_only_verified_owned_record() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    record = LiteLLMModelRecord(
        model_id=deployment.model_id,
        model_name=deployment.model_name,
        litellm_params=LiteLLMInventoryRoutingParams.from_owned(deployment.litellm_params),
        model_info=deployment.model_info.to_json(),
        server_model_info_extras={},
    )
    deleted: list[str] = []

    LiteLLMModelAdminReconciler(_DeleteApi(record, deleted)).delete_owned(deployment)

    assert deleted == [deployment.model_id]


@dataclass(frozen=True, slots=True)
class _ExtraPreservingApi(LiteLLMModelAdminApi):
    record: LiteLLMModelRecord
    updated: list[LiteLLMModelDeployment]

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        return (self.record,)

    def create_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("create must not occur")

    def update_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        self.updated.append(deployment)
        return self.record

    def delete_model(self, _: str) -> None:
        raise AssertionError("delete must not occur")


def test_update_preserves_nonrouting_server_model_info_extras() -> None:
    desired = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    static = build_owned_model_deployment(catalog_model(), bootstrap_static=True)
    record = LiteLLMModelRecord(
        model_id=desired.model_id,
        model_name=desired.model_name,
        litellm_params=LiteLLMInventoryRoutingParams.from_owned(desired.litellm_params),
        model_info={**static.model_info.to_json(), "server_note": "keep"},
        server_model_info_extras={"server_note": "keep"},
    )
    api = _ExtraPreservingApi(record, [])

    LiteLLMModelAdminReconciler(api).reconcile((desired,))

    assert len(api.updated) == 1
    model_info = api.updated[0].to_api_payload()["model_info"]
    assert isinstance(model_info, dict)
    assert model_info.get("server_note") == "keep"
