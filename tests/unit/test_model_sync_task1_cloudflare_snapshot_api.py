from __future__ import annotations

import json
from email.message import Message
from types import TracebackType
from urllib import error
from urllib import request as urllib_request

import pytest

from dokploy_wizard.networking.cloudflare import CloudflareError
from dokploy_wizard.proof import model_sync_task1_cloudflare_snapshot_api as snapshot_api
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.state import RawEnvInput


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.body = body
        self.headers = headers
        self.read_limits: list[int] = []

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        return self.body[:limit]


class FakeUrlOpen:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.timeouts: list[float] = []

    def __call__(self, _request: urllib_request.Request, *, timeout: float) -> FakeResponse:
        self.timeouts.append(timeout)
        return self.response


class FailingUrlOpen:
    def __init__(self, failure: OSError) -> None:
        self.failure = failure

    def __call__(self, _request: urllib_request.Request, *, timeout: float) -> FakeResponse:
        del timeout
        raise self.failure


def _backend() -> snapshot_api.CloudflareSnapshotApiBackend:
    return snapshot_api.CloudflareSnapshotApiBackend(
        RawEnvInput(format_version=1, values={"CLOUDFLARE_API_TOKEN": "token-secret"})
    )


_VALID_RESULT_INFO = {"page": 1, "per_page": 100, "total_count": 0, "total_pages": 1}


def _success_body(*, result: JsonValue = (), result_info: JsonValue = _VALID_RESULT_INFO) -> bytes:
    return json.dumps(
        {
            "success": True,
            "result": result,
            "result_info": result_info,
        }
    ).encode()


def test_snapshot_request_uses_finite_timeout_and_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = FakeResponse(_success_body(), {})
    fake_urlopen = FakeUrlOpen(response)
    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)

    # When
    _backend()._request("/zones/zone/dns_records", {"name": "record.example.test"})

    # Then
    assert fake_urlopen.timeouts == [snapshot_api._REQUEST_TIMEOUT_SECONDS]
    assert response.read_limits == [snapshot_api._MAX_RESPONSE_BYTES + 1]


def test_snapshot_request_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    body = b"x" * (snapshot_api._MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        FakeUrlOpen(FakeResponse(body, {"Content-Length": str(len(body))})),
    )

    # When / Then
    with pytest.raises(CloudflareError, match="incomplete or oversized"):
        _backend()._request("/accounts/account/cfd_tunnel", {})


def test_snapshot_request_rejects_advertised_body_length_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    body = _success_body()
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        FakeUrlOpen(FakeResponse(body, {"Content-Length": str(len(body) + 1)})),
    )

    # When / Then
    with pytest.raises(CloudflareError, match="incomplete or oversized"):
        _backend()._request("/accounts/account/cfd_tunnel", {})


def test_snapshot_request_accepts_missing_content_length_after_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        FakeUrlOpen(FakeResponse(_success_body(), {})),
    )

    # When
    payload = _backend()._request("/accounts/account/cfd_tunnel", {})

    # Then
    assert payload["success"] is True


def test_snapshot_list_derives_total_pages_when_tunnel_result_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_info = {
        "count": 4,
        "page": 1,
        "per_page": 100,
        "total_count": 4,
    }
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        FakeUrlOpen(FakeResponse(_success_body(result=[], result_info=result_info), {})),
    )

    page = _backend().list_tunnels_page("account", 1, 100)

    assert page.total_pages == 1


def test_snapshot_request_rejects_invalid_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        FakeUrlOpen(FakeResponse(_success_body(), {"Content-Length": "unknown"})),
    )

    # When / Then
    with pytest.raises(CloudflareError, match="incomplete or oversized"):
        _backend()._request("/accounts/account/cfd_tunnel", {})


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b'{"success":false,"result":[],"result_info":{}}',
        b'{"success":"true","result":[],"result_info":{}}',
        b'{"result":[],"result_info":{}}',
    ],
)
def test_snapshot_request_rejects_malformed_or_unsuccessful_envelopes(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    # Given
    monkeypatch.setattr(urllib_request, "urlopen", FakeUrlOpen(FakeResponse(body, {})))

    # When / Then
    with pytest.raises(CloudflareError):
        _backend()._request("/accounts/account/cfd_tunnel", {})


@pytest.mark.parametrize(
    ("result", "result_info"),
    [
        ({}, {"page": 1, "per_page": 100, "total_count": 0, "total_pages": 1}),
        ([], None),
        ([], {"page": "1", "per_page": 100, "total_count": 0, "total_pages": 1}),
    ],
)
def test_snapshot_list_rejects_malformed_result_and_result_info(
    monkeypatch: pytest.MonkeyPatch, result: JsonValue, result_info: JsonValue
) -> None:
    # Given
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        FakeUrlOpen(FakeResponse(_success_body(result=result, result_info=result_info), {})),
    )

    # When / Then
    with pytest.raises(CloudflareError):
        _backend().list_tunnels_page("account", 1, 100)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("token-secret https://api.example.test/path?query-secret raw-body-secret"),
        error.URLError("token-secret https://api.example.test/path?query-secret raw-body-secret"),
        error.HTTPError(
            "https://api.example.test/path?query-secret",
            503,
            "raw-body-secret",
            Message(),
            None,
        ),
    ],
)
def test_snapshot_request_redacts_transport_failure_details(
    monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    monkeypatch.setattr(urllib_request, "urlopen", FailingUrlOpen(failure))
    with pytest.raises(CloudflareError) as caught:
        _backend()._request("/accounts/account/cfd_tunnel", {"query": "query-secret"})
    message = str(caught.value)
    assert "token-secret" not in message
    assert "query-secret" not in message
    assert "raw-body-secret" not in message
    assert "https://" not in message


def test_null_tunnel_config_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    monkeypatch.setattr(backend, "_request", lambda _path, _query: {"result": {"config": None}})
    assert backend.get_tunnel_configuration("account", "tunnel") == ()
