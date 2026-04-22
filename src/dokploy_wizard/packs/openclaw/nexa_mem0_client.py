"""Narrow Mem0 REST adapter for Nexa runtime memory reads and writes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib import error, request

from .nexa_memory import NexaMem0Config

NexaMem0Outcome = Literal["ok", "degraded"]


@dataclass(frozen=True)
class NexaMem0DegradedError:
    """Structured degraded Mem0 failure that should not crash the worker."""

    operation: str
    reason: str
    detail: str
    retryable: bool
    status_code: int | None = None


@dataclass(frozen=True)
class NexaMem0ConfigureResult:
    """Result of ensuring the remote Mem0 server is configured."""

    outcome: NexaMem0Outcome
    configured: bool
    error: NexaMem0DegradedError | None = None


@dataclass(frozen=True)
class NexaMem0SearchHit:
    """Normalized Mem0 memory search hit."""

    memory_id: str | None
    content: str
    score: float | None
    metadata: dict[str, Any]
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class NexaMem0SearchResult:
    """Normalized search response with degraded fallback support."""

    outcome: NexaMem0Outcome
    hits: tuple[NexaMem0SearchHit, ...]
    error: NexaMem0DegradedError | None = None


@dataclass(frozen=True)
class NexaMem0WriteResult:
    """Normalized memory write response with degraded fallback support."""

    outcome: NexaMem0Outcome
    memory_id: str | None
    error: NexaMem0DegradedError | None = None


class NexaMem0Client:
    """Small JSON-over-HTTP Mem0 client for runtime memory operations."""

    def __init__(self, config: NexaMem0Config, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            msg = "Mem0 timeout_seconds must be positive."
            raise ValueError(msg)
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._base_url = config.base_url.rstrip("/")
        self._configure_result: NexaMem0ConfigureResult | None = None

    @property
    def config(self) -> NexaMem0Config:
        return self._config

    def ensure_configured(self) -> NexaMem0ConfigureResult:
        """POST the resolved Mem0 config once per client instance."""

        if self._configure_result is not None:
            return self._configure_result
        response = self._request_json("POST", "/configure", self._config.to_mem0_config())
        if response["outcome"] == "degraded":
            self._configure_result = NexaMem0ConfigureResult(
                outcome="degraded",
                configured=False,
                error=response["error"],
            )
            return self._configure_result
        self._configure_result = NexaMem0ConfigureResult(
            outcome="ok",
            configured=True,
        )
        return self._configure_result

    def search_memories(
        self,
        *,
        query: str,
        filters: dict[str, Any],
        limit: int = 5,
    ) -> NexaMem0SearchResult:
        """Search Mem0 memories with explicit filters and safe fallback behavior."""

        configured = self.ensure_configured()
        if configured.outcome == "degraded":
            return NexaMem0SearchResult(
                outcome="degraded",
                hits=(),
                error=configured.error,
            )
        response = self._request_json(
            "POST",
            "/search",
            {
                "query": query,
                "limit": limit,
                "filters": filters,
            },
        )
        if response["outcome"] == "degraded":
            return NexaMem0SearchResult(
                outcome="degraded",
                hits=(),
                error=response["error"],
            )
        payload = response["payload"]
        hits_payload = _extract_result_list(payload)
        hits: list[NexaMem0SearchHit] = []
        for item in hits_payload:
            if not isinstance(item, dict):
                continue
            hits.append(
                NexaMem0SearchHit(
                    memory_id=_first_string(item, "id", "memory_id"),
                    content=_extract_memory_content(item),
                    score=_extract_score(item),
                    metadata=_extract_metadata(item),
                    raw_payload=item,
                )
            )
        return NexaMem0SearchResult(
            outcome="ok",
            hits=tuple(hit for hit in hits if hit.content.strip() != ""),
        )

    def add_memory(
        self,
        *,
        content: str,
        user_id: str | None,
        agent_id: str,
        run_id: str | None,
        metadata: dict[str, Any],
    ) -> NexaMem0WriteResult:
        """Persist one memory candidate into Mem0 with stable scope ids."""

        configured = self.ensure_configured()
        if configured.outcome == "degraded":
            return NexaMem0WriteResult(
                outcome="degraded",
                memory_id=None,
                error=configured.error,
            )
        request_payload = {
            "memory": content,
            "messages": [{"role": "user", "content": content}],
            "agent_id": agent_id,
            "metadata": metadata,
        }
        if user_id is not None:
            request_payload["user_id"] = user_id
        if run_id is not None:
            request_payload["run_id"] = run_id
        response = self._request_json("POST", "/memories", request_payload)
        if response["outcome"] == "degraded":
            return NexaMem0WriteResult(
                outcome="degraded",
                memory_id=None,
                error=response["error"],
            )
        payload = response["payload"]
        return NexaMem0WriteResult(
            outcome="ok",
            memory_id=_first_string(payload, "id", "memory_id"),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_payload = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            url=f"{self._base_url}{path}",
            data=encoded_payload,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-API-Key": self._config.api_key,
            },
        )
        try:
            with request.urlopen(http_request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw_body = response.read()
                if raw_body == b"":
                    return {"outcome": "ok", "payload": {}}
                decoded = json.loads(raw_body)
                if isinstance(decoded, dict):
                    return {"outcome": "ok", "payload": decoded}
                return {"outcome": "ok", "payload": {"items": decoded}}
        except error.HTTPError as exc:
            detail = _decode_error_body(exc)
            return {
                "outcome": "degraded",
                "error": NexaMem0DegradedError(
                    operation=path,
                    reason="http_error",
                    detail=detail,
                    retryable=exc.code >= 500,
                    status_code=exc.code,
                ),
            }
        except error.URLError as exc:
            return {
                "outcome": "degraded",
                "error": NexaMem0DegradedError(
                    operation=path,
                    reason="transport_error",
                    detail=str(exc.reason),
                    retryable=True,
                ),
            }
        except TimeoutError as exc:
            return {
                "outcome": "degraded",
                "error": NexaMem0DegradedError(
                    operation=path,
                    reason="timeout",
                    detail=str(exc),
                    retryable=True,
                ),
            }
        except json.JSONDecodeError as exc:
            return {
                "outcome": "degraded",
                "error": NexaMem0DegradedError(
                    operation=path,
                    reason="invalid_json",
                    detail=str(exc),
                    retryable=False,
                ),
            }


def _decode_error_body(exc: error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
    except Exception:  # pragma: no cover - defensive stdlib adapter guard
        body = ""
    return body or str(exc)


def _extract_result_list(payload: dict[str, Any]) -> list[Any]:
    for key in ("results", "items", "memories", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_memory_content(payload: dict[str, Any]) -> str:
    for key in ("memory", "content", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _extract_score(payload: dict[str, Any]) -> float | None:
    value = payload.get("score")
    if isinstance(value, int | float):
        return float(value)
    return None


def _extract_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata")
    if isinstance(value, dict):
        return value
    return {}


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value
    return None


__all__ = [
    "NexaMem0Client",
    "NexaMem0ConfigureResult",
    "NexaMem0DegradedError",
    "NexaMem0SearchHit",
    "NexaMem0SearchResult",
    "NexaMem0WriteResult",
]
