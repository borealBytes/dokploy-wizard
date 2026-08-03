from __future__ import annotations

import json
from collections.abc import Callable
from email.message import Message
from typing import IO, Final, Literal
from urllib import error, request
from uuid import UUID

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMInventoryRoutingParams,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
    ModelUuid,
)

_ROUTING_KEYS: Final = frozenset(("model", "api_base", "api_key"))
_SERVER_NONROUTING_PARAM_KEYS: Final = frozenset(
    (
        "merge_reasoning_content_in_choices",
        "use_in_pass_through",
        "use_litellm_proxy",
    )
)
_OWNED_INFO_FIELDS: Final = frozenset(
    (
        "id",
        "blocked",
        "mode",
        "managed_by",
        "managed_catalog",
        "source_id",
        "transport",
        "zen_url",
        "zen_sha256",
        "zen_observed_at",
        "models_dev_url",
        "models_dev_sha256",
        "models_dev_pricing_row_sha256",
        "models_dev_provider_npm",
        "models_dev_observed_at",
        "official_transport_url",
        "official_transport_commit",
        "official_transport_blob_sha256",
        "official_transport_row_sha256",
        "official_pricing_url",
        "official_pricing_commit",
        "official_pricing_blob_sha256",
        "official_pricing_row_sha256",
        "merged_decision_sha256",
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
        "max_input_tokens",
        "max_output_tokens",
        "dokploy_source_input_per_million",
        "dokploy_source_output_per_million",
        "dokploy_source_cache_read_per_million",
        "dokploy_source_cache_write_per_million",
        "dokploy_scalar_input_per_million",
        "dokploy_scalar_output_per_million",
        "dokploy_scalar_cache_read_per_million",
        "dokploy_scalar_cache_write_per_million",
        "dokploy_pricing_tiers_sha256",
        "dokploy_pricing_selection_sha256",
        "dokploy_pricing_input_provenance",
        "dokploy_pricing_output_provenance",
        "dokploy_pricing_cache_read_provenance",
        "dokploy_pricing_cache_write_provenance",
        "dokploy_spec_sha256",
        "bootstrap_static",
    )
)


class LiteLLMModelAdminClient:
    def __init__(
        self,
        *,
        api_url: str,
        master_key: str,
        request_fn: Callable[[request.Request], JsonValue] | None = None,
    ) -> None:
        self._api_url = api_url.removesuffix("/")
        self._master_key = master_key
        self._request_fn = request_fn or _default_request

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        payload = self._request_json("GET", "/v1/model/info")
        root = _expect_object(payload, "model inventory")
        data = root.get("data")
        if not isinstance(data, list):
            raise LiteLLMModelAdminError("LiteLLM model inventory requires a data array")
        return tuple(_parse_record(item, context="inventory", inventory=True) for item in data)

    def create_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        payload = self._request_json("POST", "/model/new", deployment.to_api_payload())
        return _parse_stored_record(payload, deployment)

    def update_model(self, deployment: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        payload = self._request_json(
            "PATCH",
            f"/model/{deployment.model_id}/update",
            deployment.to_api_payload(),
        )
        return _parse_stored_record(payload, deployment)

    def delete_model(self, model_id: ModelUuid) -> None:
        payload = self._request_json("POST", "/model/delete", {"id": model_id})
        root = _expect_object(payload, "model delete")
        if root != {"message": f"Model: {model_id} deleted successfully"}:
            raise LiteLLMModelAdminError("LiteLLM model delete response did not confirm success")

    def _request_json(
        self,
        method: Literal["GET", "POST", "PATCH"],
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._master_key}",
        }
        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        raw_request = request.Request(
            url=f"{self._api_url}{path}",
            method=method,
            headers=headers,
            data=data,
        )
        try:
            return self._request_fn(raw_request)
        except error.HTTPError as exc:
            raise LiteLLMModelAdminError(
                f"LiteLLM model admin request failed with status {exc.code}"
            ) from exc
        except error.URLError as exc:
            if method == "GET":
                raise LiteLLMModelAdminError("LiteLLM model admin read transport failed") from exc
            operation: Literal["create", "update"] = "create" if method == "POST" else "update"
            raise LiteLLMModelAdminWriteAmbiguity(f"ambiguous {operation}") from exc


def _parse_stored_record(
    payload: JsonValue, deployment: LiteLLMModelDeployment
) -> LiteLLMModelRecord:
    record = _parse_record(
        payload,
        context="stored response",
        require_model_id=True,
        inventory=False,
    )
    if record.model_id != deployment.model_id:
        raise LiteLLMModelAdminError("LiteLLM model path/body id mismatch")
    if record.model_name != deployment.model_name:
        raise LiteLLMModelAdminError("LiteLLM model response name mismatch")
    return record


def _parse_record(
    payload: JsonValue,
    *,
    context: str,
    require_model_id: bool = False,
    inventory: bool,
) -> LiteLLMModelRecord:
    record = _expect_object(payload, f"LiteLLM {context} deployment")
    if "blocked" in record:
        raise LiteLLMModelAdminError("LiteLLM deployment blocked must be nested in model_info")
    model_name = _expect_nonempty_string(record.get("model_name"), f"LiteLLM {context} model_name")
    params = _parse_routing(record.get("litellm_params"), context, inventory=inventory)
    model_info = _expect_object(record.get("model_info"), f"LiteLLM {context} model_info")
    model_id = _parse_model_id(model_info.get("id"), f"LiteLLM {context} model_info.id")
    if require_model_id:
        response_id = _parse_model_id(record.get("model_id"), f"LiteLLM {context} model_id")
        if response_id != model_id:
            raise LiteLLMModelAdminError("LiteLLM model path/body id mismatch")
    extras = {key: value for key, value in model_info.items() if key not in _OWNED_INFO_FIELDS}
    return LiteLLMModelRecord(
        model_id=model_id,
        model_name=model_name,
        litellm_params=params,
        model_info=dict(model_info),
        server_model_info_extras=extras,
    )


def _parse_routing(
    payload: JsonValue | None, context: str, *, inventory: bool
) -> LiteLLMInventoryRoutingParams:
    params = _expect_object(payload, f"LiteLLM {context} litellm_params")
    parameter_names = frozenset(params)
    server_parameter_names = parameter_names - _ROUTING_KEYS
    if not server_parameter_names <= _SERVER_NONROUTING_PARAM_KEYS or any(
        not isinstance(params[name], bool) for name in server_parameter_names
    ):
        raise LiteLLMModelAdminError(
            "LiteLLM model routing parameters must contain exactly three keys"
        )
    routing_parameter_names = parameter_names - server_parameter_names
    inventory_parameter_names = _ROUTING_KEYS - {"api_key"}
    has_removed_api_key = "api_key" not in routing_parameter_names
    if inventory and routing_parameter_names == inventory_parameter_names and has_removed_api_key:
        return LiteLLMInventoryRoutingParams(
            model=_expect_nonempty_string(params["model"], "LiteLLM routing model"),
            api_base=_expect_nonempty_string(params["api_base"], "LiteLLM routing api_base"),
            api_key=None,
        )
    if routing_parameter_names != _ROUTING_KEYS:
        raise LiteLLMModelAdminError(
            "LiteLLM model routing parameters must contain exactly three keys"
        )
    api_key = _expect_nonempty_string(params["api_key"], "LiteLLM routing api_key")
    if api_key.startswith("*"):
        raise LiteLLMModelAdminError("LiteLLM model inventory contains a masked routing parameter")
    return LiteLLMInventoryRoutingParams(
        model=_expect_nonempty_string(params["model"], "LiteLLM routing model"),
        api_base=_expect_nonempty_string(params["api_base"], "LiteLLM routing api_base"),
        api_key=api_key,
    )


def _expect_object(payload: JsonValue | None, context: str) -> dict[str, JsonValue]:
    if not isinstance(payload, dict):
        raise LiteLLMModelAdminError(f"{context} must be a JSON object")
    return payload


def _expect_nonempty_string(value: JsonValue | None, context: str) -> str:
    if not isinstance(value, str) or value == "":
        raise LiteLLMModelAdminError(f"{context} must be a non-empty string")
    return value


def _parse_model_id(value: JsonValue | None, context: str) -> ModelUuid:
    raw_model_id = _expect_nonempty_string(value, context)
    try:
        parsed = UUID(raw_model_id)
    except ValueError as exc:
        raise LiteLLMModelAdminError(f"{context} must be a canonical UUID") from exc
    if str(parsed) != raw_model_id:
        raise LiteLLMModelAdminError(f"{context} must be a canonical UUID")
    return ModelUuid(raw_model_id)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        new_url: str,
    ) -> request.Request | None:
        del req, fp, code, msg, headers, new_url
        return None


def _default_request(raw_request: request.Request) -> JsonValue:
    opener = request.build_opener(_NoRedirect())
    with opener.open(raw_request, timeout=30) as response:
        return _json_value(json.loads(response.read().decode("utf-8")))


def _json_value(value: JsonValue) -> JsonValue:
    return value
