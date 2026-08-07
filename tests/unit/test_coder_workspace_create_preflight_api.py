from __future__ import annotations

from types import TracebackType
from typing import Self
from urllib import request

import pytest

from dokploy_wizard.proof import model_sync_coder_api


class _PayloadResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._payload


class _FailingOpener:
    def open(self, _request: request.Request, *, timeout: int) -> _PayloadResponse:
        assert timeout == 30
        raise OSError("opaque")


class _PayloadOpener:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def open(self, _request: request.Request, *, timeout: int) -> _PayloadResponse:
        assert timeout == 30
        return _PayloadResponse(self._payload)


def test_api_classifies_opener_failure_as_endpoint_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(model_sync_coder_api, "active_task1_proof_context", lambda: None)
    monkeypatch.setattr(request, "build_opener", lambda *_handlers: _FailingOpener())

    # When / Then
    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api.api("coder.example.test", None, "/api/v2/buildinfo")

    assert error.value.stage == "endpoint"
    assert "opaque" not in str(error.value)


@pytest.mark.parametrize("payload", [b"not-json", b"x" * (2 * 1024 * 1024 + 1)])
def test_api_classifies_response_payload_failure_at_api_seam(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    # Given
    monkeypatch.setattr(model_sync_coder_api, "active_task1_proof_context", lambda: None)
    monkeypatch.setattr(request, "build_opener", lambda *_handlers: _PayloadOpener(payload))

    # When / Then
    with pytest.raises(model_sync_coder_api.CoderSnapshotApiError) as error:
        model_sync_coder_api.api("coder.example.test", None, "/api/v2/buildinfo")

    assert error.value.stage == "payload"
