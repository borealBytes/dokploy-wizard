# ruff: noqa: E501
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib import error, parse, request


class LiteLLMAdminError(RuntimeError):
    """Raised when LiteLLM admin API requests fail."""


class LiteLLMReadinessError(LiteLLMAdminError):
    """Raised when LiteLLM never becomes ready."""


@dataclass(frozen=True)
class LiteLLMTeamRecord:
    team_id: str
    team_alias: str
    models: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LiteLLMVirtualKeyRecord:
    key: str
    key_alias: str
    team_id: str | None
    models: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)


class LiteLLMAdminApi(Protocol):
    def readiness(self) -> dict[str, Any]: ...

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]: ...

    def create_team(
        self,
        *,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord: ...

    def update_team(
        self,
        *,
        team_id: str,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord: ...

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]: ...

    def create_key(
        self,
        *,
        key: str,
        key_alias: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord: ...

    def update_key(
        self,
        *,
        key_alias: str,
        key: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord: ...


class LiteLLMAdminClient:
    _KEY_LIST_PAGE_SIZE = 100

    def __init__(
        self,
        *,
        api_url: str,
        master_key: str,
        request_fn: Callable[[request.Request], Any] | None = None,
    ) -> None:
        self._api_url = api_url.removesuffix("/")
        self._master_key = master_key
        self._request_fn = request_fn or _default_request

    def readiness(self) -> dict[str, Any]:
        payload = self._request_json("GET", "/health/readiness", auth=False)
        if not isinstance(payload, dict):
            raise LiteLLMAdminError("LiteLLM readiness response must be a JSON object.")
        return payload

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        payload = self._request_json("GET", "/team/list")
        if not isinstance(payload, list):
            raise LiteLLMAdminError("LiteLLM team.list response must be a list.")
        return tuple(_parse_team(item) for item in payload)

    def create_team(
        self,
        *,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        payload = self._request_json(
            "POST",
            "/team/new",
            {"team_alias": team_alias, "models": list(models), "metadata": dict(metadata or {})},
        )
        return _parse_team(
            _unwrap_record_response(
                payload,
                response_kind="team.new",
                required_string_fields=("team_alias", "team_alias_name", "alias"),
                container_keys=("team", "team_info", "data", "result", "record", "item"),
                allowed_record_fields=("team_id", "teamId", "models", "metadata"),
            ),
            fallback_alias=team_alias,
        )

    def update_team(
        self,
        *,
        team_id: str,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        payload = self._request_json(
            "POST",
            "/team/update",
            {
                "team_id": team_id,
                "team_alias": team_alias,
                "models": list(models),
                "metadata": dict(metadata or {}),
            },
        )
        return _parse_team(
            _unwrap_record_response(
                payload,
                response_kind="team.update",
                required_string_fields=("team_alias", "team_alias_name", "alias"),
                container_keys=("team", "team_info", "data", "result", "record", "item"),
                allowed_record_fields=("team_id", "teamId", "models", "metadata"),
            ),
            fallback_alias=team_alias,
            fallback_team_id=team_id,
        )

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        records: list[LiteLLMVirtualKeyRecord] = []
        page = 1
        while True:
            query = parse.urlencode(
                {
                    "page": page,
                    "size": self._KEY_LIST_PAGE_SIZE,
                    "return_full_object": "true",
                }
            )
            payload = self._request_json("GET", f"/key/list?{query}")
            page_items = _parse_key_list_payload(payload)
            records.extend(_parse_key(item) for item in page_items)
            if len(page_items) < self._KEY_LIST_PAGE_SIZE:
                return tuple(records)
            page += 1

    def create_key(
        self,
        *,
        key: str,
        key_alias: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        payload = self._request_json(
            "POST",
            "/key/generate",
            {
                "key": key,
                "key_alias": key_alias,
                "team_id": team_id,
                "models": list(models),
                "metadata": dict(metadata or {}),
            },
        )
        return _parse_key(
            _unwrap_record_response(
                payload,
                response_kind="key.generate",
                required_string_fields=("key", "token", "api_key", "key_alias", "key_name", "alias"),
                container_keys=("key", "token", "virtual_key", "data", "result", "record", "item"),
                allowed_record_fields=("team_id", "teamId", "models", "metadata"),
            ),
            fallback_key=key,
            fallback_alias=key_alias,
            fallback_team_id=team_id,
        )

    def update_key(
        self,
        *,
        key_alias: str,
        key: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        payload = self._request_json(
            "POST",
            "/key/update",
            {
                "key_alias": key_alias,
                "key": key,
                "team_id": team_id,
                "models": list(models),
                "metadata": dict(metadata or {}),
            },
        )
        return _parse_key(
            _unwrap_record_response(
                payload,
                response_kind="key.update",
                required_string_fields=("key", "token", "api_key", "key_alias", "key_name", "alias"),
                container_keys=("key", "token", "virtual_key", "data", "result", "record", "item"),
                allowed_record_fields=("team_id", "teamId", "models", "metadata"),
            ),
            fallback_key=key,
            fallback_alias=key_alias,
            fallback_team_id=team_id,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        auth: bool = True,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._master_key}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(
            url=f"{self._api_url}{path}",
            method=method,
            headers=headers,
            data=data,
        )
        try:
            return self._request_fn(req)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LiteLLMAdminError(
                f"LiteLLM admin API request failed with status {exc.code}: {body or exc.reason}."
            ) from exc
        except error.URLError as exc:
            raise LiteLLMAdminError(f"LiteLLM admin API request failed: {exc.reason}.") from exc


class LiteLLMGatewayManager:
    def __init__(
        self,
        *,
        api: LiteLLMAdminApi,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self._sleep_fn = sleep_fn

    def wait_until_ready(self, *, attempts: int = 120, delay_seconds: float = 5.0) -> None:
        last_snapshot: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                last_snapshot = self._api.readiness()
                if _readiness_is_healthy(last_snapshot):
                    return
            except Exception as exc:
                last_error = exc
            if attempt < attempts - 1:
                self._sleep_fn(delay_seconds)
        detail = _readiness_failure_detail(last_snapshot, last_error)
        raise LiteLLMReadinessError(
            "LiteLLM did not become ready before the timeout. "
            f"Check /health/readiness, LiteLLM logs, and shared-core Postgres connectivity. {detail}"
        )

    def reconcile_virtual_keys(
        self,
        *,
        generated_keys: Mapping[str, str],
        consumer_model_allowlists: Mapping[str, tuple[str, ...]],
    ) -> dict[str, LiteLLMVirtualKeyRecord]:
        existing_teams = {team.team_alias: team for team in self._api.list_teams()}
        existing_keys = {record.key_alias: record for record in self._api.list_keys()}
        reconciled: dict[str, LiteLLMVirtualKeyRecord] = {}
        for consumer, generated_key in generated_keys.items():
            expected_models = tuple(dict.fromkeys(consumer_model_allowlists.get(consumer, ())))
            managed_metadata = _managed_metadata_for_consumer(consumer)
            team = existing_teams.get(consumer)
            if team is None:
                team = self._api.create_team(
                    team_alias=consumer,
                    models=expected_models,
                    metadata=managed_metadata,
                )
                existing_teams[consumer] = team
            elif team.models != expected_models:
                _ensure_record_is_wizard_managed(
                    record_kind="team",
                    consumer=consumer,
                    metadata=team.metadata,
                )
                team = self._api.update_team(
                    team_id=team.team_id,
                    team_alias=consumer,
                    models=expected_models,
                    metadata=managed_metadata,
                )
                existing_teams[consumer] = team

            existing_key = existing_keys.get(consumer)
            if existing_key is None:
                existing_key = self._api.create_key(
                    key=generated_key,
                    key_alias=consumer,
                    team_id=team.team_id,
                    models=expected_models,
                    metadata=managed_metadata,
                )
                existing_keys[consumer] = existing_key
            elif existing_key.key != generated_key:
                if existing_key.models != expected_models or existing_key.team_id != team.team_id:
                    _ensure_record_is_wizard_managed(
                        record_kind="key",
                        consumer=consumer,
                        metadata=existing_key.metadata,
                    )
                    existing_key = self._api.update_key(
                        key_alias=consumer,
                        key=existing_key.key,
                        team_id=team.team_id,
                        models=expected_models,
                        metadata=managed_metadata,
                    )
                    existing_keys[consumer] = existing_key
                    reconciled[consumer] = existing_key
                    continue
                existing_key = LiteLLMVirtualKeyRecord(
                    key=existing_key.key,
                    key_alias=existing_key.key_alias,
                    team_id=team.team_id,
                    models=expected_models,
                    metadata=existing_key.metadata,
                )
            elif existing_key.models != expected_models or existing_key.team_id != team.team_id:
                _ensure_record_is_wizard_managed(
                    record_kind="key",
                    consumer=consumer,
                    metadata=existing_key.metadata,
                )
                existing_key = self._api.update_key(
                    key_alias=consumer,
                    key=existing_key.key,
                    team_id=team.team_id,
                    models=expected_models,
                    metadata=managed_metadata,
                )
                existing_keys[consumer] = existing_key
            reconciled[consumer] = existing_key
        return reconciled


def _readiness_is_healthy(snapshot: Mapping[str, Any]) -> bool:
    status = snapshot.get("status")
    db_status = snapshot.get("db")
    return status in ("connected", "healthy") and db_status == "connected"


def _readiness_failure_detail(
    snapshot: Mapping[str, Any] | None, error_value: Exception | None
) -> str:
    if snapshot is not None:
        status = snapshot.get("status", "unknown")
        db_status = snapshot.get("db", "unknown")
        return f"Last readiness payload reported status={status!r}, db={db_status!r}."
    if error_value is not None:
        return str(error_value)
    return "No readiness response was received."


def _parse_team(
    payload: Any,
    *,
    fallback_alias: str | None = None,
    fallback_team_id: str | None = None,
) -> LiteLLMTeamRecord:
    if not isinstance(payload, dict):
        raise LiteLLMAdminError("LiteLLM team record must be an object.")
    team_alias = _first_string(payload, "team_alias", "team_alias_name", "alias", default=fallback_alias)
    team_id = _first_string(payload, "team_id", "teamId", default=fallback_team_id or team_alias)
    return LiteLLMTeamRecord(
        team_id=team_id,
        team_alias=team_alias,
        models=_tuple_of_strings(payload.get("models")),
        metadata=_metadata_mapping(payload.get("metadata")),
    )


def _parse_key(
    payload: Any,
    *,
    fallback_key: str | None = None,
    fallback_alias: str | None = None,
    fallback_team_id: str | None = None,
) -> LiteLLMVirtualKeyRecord:
    if not isinstance(payload, dict):
        raise LiteLLMAdminError("LiteLLM key record must be an object.")
    key = _first_string(payload, "key", "token", "api_key", default=fallback_key)
    key_alias = _first_string(payload, "key_alias", "key_name", "alias", default=fallback_alias)
    team_id = _optional_string(payload, "team_id", "teamId") or fallback_team_id
    return LiteLLMVirtualKeyRecord(
        key=key,
        key_alias=key_alias,
        team_id=team_id,
        models=_tuple_of_strings(payload.get("models")),
        metadata=_metadata_mapping(payload.get("metadata")),
    )


def _parse_key_list_payload(payload: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(payload, list):
        return tuple(_ensure_key_payload(item) for item in payload)
    if isinstance(payload, dict):
        for candidate_key in ("keys", "data", "items", "results"):
            candidate_value = payload.get(candidate_key)
            if isinstance(candidate_value, list):
                return tuple(_ensure_key_payload(item) for item in candidate_value)
        raise LiteLLMAdminError(
            "LiteLLM key.list response must contain a list under one of "
            "('keys', 'data', 'items', 'results') when the top-level payload is an object."
        )
    raise LiteLLMAdminError("LiteLLM key.list response must be a list or paginated object.")


def _ensure_key_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LiteLLMAdminError("LiteLLM key record must be an object.")
    return payload


def _unwrap_record_response(
    payload: Any,
    *,
    response_kind: str,
    required_string_fields: tuple[str, ...],
    container_keys: tuple[str, ...],
    allowed_record_fields: tuple[str, ...],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LiteLLMAdminError(f"LiteLLM {response_kind} response must be an object.")
    if _optional_string(payload, *required_string_fields) is not None or any(
        field in payload for field in allowed_record_fields
    ):
        return payload
    for container_key in container_keys:
        candidate = payload.get(container_key)
        if isinstance(candidate, dict) and (
            _optional_string(candidate, *required_string_fields) is not None
            or any(field in candidate for field in allowed_record_fields)
        ):
            return candidate
    raise LiteLLMAdminError(
        f"LiteLLM {response_kind} response missing required string field from {required_string_fields!r}. "
        f"Checked the top-level payload and nested objects under {container_keys!r}."
    )


def _first_string(payload: Mapping[str, Any], *keys: str, default: str | None = None) -> str:
    value = _optional_string(payload, *keys)
    if value is not None:
        return value
    if default is not None:
        return default
    raise LiteLLMAdminError(f"LiteLLM response missing required string field from {keys!r}.")


def _optional_string(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()
    return None


def _tuple_of_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip() != "")


def _metadata_mapping(value: Any) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): nested for key, nested in value.items()}


def _managed_metadata_for_consumer(consumer: str) -> dict[str, object]:
    return {"consumer": consumer, "managed_by": "dokploy-wizard"}


def _ensure_record_is_wizard_managed(
    *,
    record_kind: str,
    consumer: str,
    metadata: Mapping[str, object],
) -> None:
    if metadata.get("managed_by") != "dokploy-wizard":
        raise LiteLLMAdminError(
            f"LiteLLM {record_kind} '{consumer}' drifted but is not wizard-managed. "
            "Refusing to mutate it silently."
        )
    metadata_consumer = metadata.get("consumer")
    if metadata_consumer != consumer:
        raise LiteLLMAdminError(
            f"LiteLLM {record_kind} '{consumer}' drifted but metadata belongs to consumer {metadata_consumer!r}. "
            "Refusing to mutate it silently."
        )


def _default_request(req: request.Request) -> Any:
    with request.urlopen(req, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))
