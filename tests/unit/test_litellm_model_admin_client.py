from __future__ import annotations

from email.message import Message
from urllib import error, request

import pytest

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.model_admin import (
    LiteLLMModelAdminClient,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
    build_owned_model_deployment,
)
from tests.unit._litellm_model_admin_support import catalog_model


def _stored_row_with_parameters(
    parameters: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    return {
        "model_id": deployment.model_id,
        "model_name": deployment.model_name,
        "litellm_params": parameters,
        "model_info": deployment.model_info.to_json(),
    }


def test_stored_response_allows_only_pinned_server_nonrouting_parameter_extras() -> None:
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)
    pinned_parameters = {
        **deployment.litellm_params.to_json(),
        "merge_reasoning_content_in_choices": False,
        "use_in_pass_through": False,
        "use_litellm_proxy": False,
    }

    def known_extra_request(_: request.Request) -> JsonValue:
        return _stored_row_with_parameters(pinned_parameters)

    stored = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=known_extra_request,
    ).create_model(deployment)

    assert stored.litellm_params.model == deployment.litellm_params.model

    def unknown_extra_request(_: request.Request) -> JsonValue:
        return _stored_row_with_parameters({**pinned_parameters, "unexpected": True})

    client = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=unknown_extra_request,
    )
    with pytest.raises(LiteLLMModelAdminError, match="routing parameters"):
        client.create_model(deployment)


def test_create_http_500_is_treated_as_write_ambiguity() -> None:
    # Given
    deployment = build_owned_model_deployment(catalog_model(), bootstrap_static=False)

    def failed_request(raw_request: request.Request) -> JsonValue:
        raise error.HTTPError(raw_request.full_url, 500, "", Message(), None)

    client = LiteLLMModelAdminClient(
        api_url="http://litellm.internal",
        master_key="test-master-key",
        request_fn=failed_request,
    )

    # When / Then
    with pytest.raises(LiteLLMModelAdminWriteAmbiguity, match="ambiguous create"):
        client.create_model(deployment)
