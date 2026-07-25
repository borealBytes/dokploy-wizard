from __future__ import annotations

import hashlib
import json
from urllib import request

import pytest

from dokploy_wizard.dokploy import DokployApiClient
from dokploy_wizard.dokploy.names import InvalidDokployAppNameError


def _compose_create_payload(app_name: str) -> dict[str, str | None]:
    requests_seen: list[dict[str, str | None]] = []

    def fake_request(req: request.Request) -> dict[str, dict[str, str]]:
        body = req.data
        assert isinstance(body, bytes)
        payload = json.loads(body.decode("utf-8"))
        assert isinstance(payload, dict)
        requests_seen.append(payload)
        if req.full_url.endswith("/api/compose.create"):
            return {"data": {"composeId": "cmp-1", "name": app_name}}
        if req.full_url.endswith("/api/compose.update"):
            return {"data": {"composeId": "cmp-1", "name": app_name}}
        raise AssertionError(req.full_url)

    client = DokployApiClient(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-key-123",
        request_fn=fake_request,
    )

    client.create_compose(
        name=app_name,
        environment_id="env-1",
        compose_file="services: {}\n",
        app_name=app_name,
    )
    return requests_seen[0]


def test_compose_create_bounds_context_auth_probe_app_name() -> None:
    raw_app_name = f"{'s' * 38}-dokploy-wizard-auth-probe"

    payload = _compose_create_payload(raw_app_name)

    assert len(raw_app_name) == 64
    assert payload["name"] == raw_app_name
    sent_app_name = payload["appName"]
    assert isinstance(sent_app_name, str)
    assert len(sent_app_name) <= 56


@pytest.mark.parametrize("length", [55, 56, 57, 64])
def test_compose_create_app_name_obeys_dokploy_input_boundary(length: int) -> None:
    raw_app_name = "a" * length
    expected = raw_app_name
    if length > 56:
        digest = hashlib.sha256(raw_app_name.encode("ascii")).hexdigest()[:24]
        expected = f"{raw_app_name[:31]}-{digest}"

    payload = _compose_create_payload(raw_app_name)

    assert payload["appName"] == expected


def test_compose_create_keeps_distinct_hashes_for_long_names_with_same_prefix() -> None:
    first = _compose_create_payload(f"{'a' * 56}b")["appName"]
    second = _compose_create_payload(f"{'a' * 56}c")["appName"]

    assert first != second


@pytest.mark.parametrize(
    "invalid_name",
    ["", "-leading", "trailing-", "double--hyphen", "Uppercase", "under_score"],
)
def test_compose_create_rejects_noncanonical_app_name(invalid_name: str) -> None:
    with pytest.raises(InvalidDokployAppNameError):
        _compose_create_payload(invalid_name)
