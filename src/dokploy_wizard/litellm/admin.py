from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True)
class LiteLLMVirtualKeyRecord:
    key: str
    key_alias: str
    team_id: str | None
    models: tuple[str, ...]


class LiteLLMAdminApi(Protocol):
    def readiness(self) -> dict[str, Any]: ...

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]: ...

    def create_team(self, *, team_alias: str, models: tuple[str, ...]) -> LiteLLMTeamRecord: ...

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


class LiteLLMAdminClient:
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

    def create_team(self, *, team_alias: str, models: tuple[str, ...]) -> LiteLLMTeamRecord:
        payload = self._request_json(
            "POST",
            "/team/new",
            {"team_alias": team_alias, "models": list(models)},
        )
        if not isinstance(payload, dict):
            raise LiteLLMAdminError("LiteLLM team.new response must be an object.")
        return _parse_team(payload)

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        query = parse.urlencode({"page": 1, "size": 1000, "return_full_object": "true"})
        payload = self._request_json("GET", f"/key/list?{query}")
        if not isinstance(payload, list):
            raise LiteLLMAdminError("LiteLLM key.list response must be a list.")
        return tuple(_parse_key(item) for item in payload)

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
        if not isinstance(payload, dict):
            raise LiteLLMAdminError("LiteLLM key.generate response must be an object.")
        return _parse_key(payload, fallback_key=key, fallback_alias=key_alias, fallback_team_id=team_id)

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

    def wait_until_ready(self, *, attempts: int = 20, delay_seconds: float = 3.0) -> None:
        last_snapshot: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                last_snapshot = self._api.readiness()
                if _readiness_is_healthy(last_snapshot):
                    return
            except LiteLLMAdminError as exc:
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
            team = existing_teams.get(consumer)
            if team is None:
                team = self._api.create_team(team_alias=consumer, models=expected_models)
                existing_teams[consumer] = team
            elif team.models != expected_models:
                raise LiteLLMAdminError(
                    f"LiteLLM team '{consumer}' already exists with models {list(team.models)}, "
                    f"expected {list(expected_models)}. Refusing to mutate team restrictions silently."
                )

            existing_key = existing_keys.get(consumer)
            if existing_key is None:
                existing_key = self._api.create_key(
                    key=generated_key,
                    key_alias=consumer,
                    team_id=team.team_id,
                    models=expected_models,
                    metadata={"consumer": consumer, "managed_by": "dokploy-wizard"},
                )
                existing_keys[consumer] = existing_key
            elif existing_key.key != generated_key:
                raise LiteLLMAdminError(
                    f"LiteLLM key alias '{consumer}' already exists with a different key value. "
                    "Refusing to rotate or overwrite the existing virtual key silently."
                )
            elif existing_key.models != expected_models:
                raise LiteLLMAdminError(
                    f"LiteLLM key alias '{consumer}' already exists with models {list(existing_key.models)}, "
                    f"expected {list(expected_models)}. Refusing to mutate key restrictions silently."
                )
            elif existing_key.team_id != team.team_id:
                raise LiteLLMAdminError(
                    f"LiteLLM key alias '{consumer}' already exists on team '{existing_key.team_id}', "
                    f"expected '{team.team_id}'. Refusing to reassign it silently."
                )
            reconciled[consumer] = existing_key
        return reconciled


def _readiness_is_healthy(snapshot: Mapping[str, Any]) -> bool:
    status = snapshot.get("status")
    db_status = snapshot.get("db")
    return status == "connected" and db_status == "connected"


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


def _parse_team(payload: Any) -> LiteLLMTeamRecord:
    if not isinstance(payload, dict):
        raise LiteLLMAdminError("LiteLLM team record must be an object.")
    team_alias = _first_string(payload, "team_alias", "team_alias_name", "alias")
    team_id = _first_string(payload, "team_id", "teamId", default=team_alias)
    return LiteLLMTeamRecord(
        team_id=team_id,
        team_alias=team_alias,
        models=_tuple_of_strings(payload.get("models")),
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


def _default_request(req: request.Request) -> Any:
    with request.urlopen(req, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))
