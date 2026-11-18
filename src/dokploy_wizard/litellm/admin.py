# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib import error, parse, request

from dokploy_wizard.identity import username_from_email
from dokploy_wizard.state.models import LITELLM_WORKSPACE_ISSUER_CONSUMER


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
    # Live /key/list payloads carry no raw token material: `token` is the
    # sha256 hex of the accepted key (measured on the box). Records parsed
    # from such payloads set this field so callers can prove a match without
    # the raw value.
    token: str | None = None


def _matches_generated_key(record: LiteLLMVirtualKeyRecord, generated_key: str) -> bool:
    """True when the record proves the wizard's raw key.

    A gateway that echoes the raw value matches directly. The live LiteLLM
    ``/key/list?return_full_object=true`` payload carries no raw key at all
    (only the masked ``key_name`` and the hashed ``token``), so the hash form
    is the proof: ``token == sha256(raw)`` is the only comparison that can
    converge on a real box — comparing the raw state value to the payload's
    ``key`` (which falls back to the hash) never matches.
    """
    if record.key == generated_key:
        return True
    return record.token is not None and hashlib.sha256(generated_key.encode()).hexdigest() == record.token


# The grant a workspace-minted key (template MINTPY lane) carries when its
# owner has model access: the model-sync wildcard plus the synced default-chat
# alias. The template hardcodes the same pair — test_coder_pack pins both
# sides so they cannot drift apart silently.
WORKSPACE_KEY_GRANTED_MODELS: tuple[str, ...] = ("sync/*", "sync/default-chat")
# The deny lane is a real key scoped to one model name the gateway does not
# deploy. The workspace still gets its personal key (boot stays healthy,
# spend stays attributed); inference fails honestly at the gateway with a
# model-not-found error instead of the fallback leaking the shared baked key.
WORKSPACE_KEY_DENY_MODEL = "dokploy-wizard-model-access-denied"


@dataclass(frozen=True)
class LiteLLMWorkspaceKeyPolicy:
    """Who may hold a workspace-minted key with the operator's LiteLLM models.

    Brand-new Coder users are denied by default; ``allow_new_users`` grants
    everyone, ``allowed_users`` names logins, and the seeded admin (derived
    from the operator admin email, the same normalization the first-user lane
    used) is always exempt. A key minted for a denied owner scopes to
    ``WORKSPACE_KEY_DENY_MODEL`` so boot stays healthy and inference fails at
    the gateway rather than falling back to the shared baked key.
    """

    allow_new_users: bool
    allowed_users: frozenset[str]
    admin_login: str

    def models_for_owner(self, owner: str) -> tuple[str, ...]:
        login = str(owner).strip().lower()
        if (
            self.allow_new_users
            or login in self.allowed_users
            or login == self.admin_login
        ):
            return WORKSPACE_KEY_GRANTED_MODELS
        return (WORKSPACE_KEY_DENY_MODEL,)


def build_litellm_workspace_key_policy(
    *,
    allow_new_users: bool,
    allowed_users: Iterable[str],
    admin_email: str,
) -> LiteLLMWorkspaceKeyPolicy:
    return LiteLLMWorkspaceKeyPolicy(
        allow_new_users=allow_new_users,
        allowed_users=frozenset(
            str(user).strip().lower() for user in allowed_users if str(user).strip()
        ),
        admin_login=username_from_email(admin_email),
    )


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
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord: ...

    def update_key(
        self,
        *,
        key_alias: str,
        key: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord: ...

    def delete_key(self, *, key_alias: str) -> None: ...
    def delete_team(self, *, team_id: str) -> None: ...


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
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        body: dict[str, object] = {
            "key": key,
            "key_alias": key_alias,
            "team_id": team_id,
            "models": list(models),
            "metadata": dict(metadata or {}),
        }
        if allowed_routes is not None:
            body["allowed_routes"] = list(allowed_routes)
        payload = self._request_json("POST", "/key/generate", body)
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
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        body: dict[str, object] = {
            "key_alias": key_alias,
            "key": key,
            "team_id": team_id,
            "models": list(models),
            "metadata": dict(metadata or {}),
        }
        if allowed_routes is not None:
            body["allowed_routes"] = list(allowed_routes)
        payload = self._request_json("POST", "/key/update", body)
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

    def delete_key(self, *, key_alias: str) -> None:
        self._request_json("POST", "/key/delete", {"key_aliases": [key_alias]})

    def delete_team(self, *, team_id: str) -> None:
        self._request_json("POST", "/team/delete", {"team_ids": [team_id]})

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


WORKSPACE_ISSUER_ALLOWED_ROUTES: tuple[str, ...] = (
    "/key/generate",
    "/key/info",
    "/key/delete",
)


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
        workspace_key_policy: LiteLLMWorkspaceKeyPolicy | None = None,
    ) -> dict[str, LiteLLMVirtualKeyRecord]:
        existing_teams = {team.team_alias: team for team in self._api.list_teams()}
        existing_keys = {record.key_alias: record for record in self._api.list_keys()}
        reconciled: dict[str, LiteLLMVirtualKeyRecord] = {}
        for consumer, generated_key in generated_keys.items():
            expected_models = _expected_models_for_consumer(
                consumer,
                consumer_model_allowlists,
            )
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
            key_routes = (
                WORKSPACE_ISSUER_ALLOWED_ROUTES
                if consumer == LITELLM_WORKSPACE_ISSUER_CONSUMER
                else None
            )
            if existing_key is None:
                existing_key = self._api.create_key(
                    key=generated_key,
                    key_alias=consumer,
                    team_id=team.team_id,
                    models=expected_models,
                    metadata=managed_metadata,
                    allowed_routes=key_routes,
                )
                existing_keys[consumer] = existing_key
            else:
                key_value_drifted = not _matches_generated_key(existing_key, generated_key)
                key_scope_drifted = (
                    existing_key.models != expected_models or existing_key.team_id != team.team_id
                )

                if key_value_drifted or key_scope_drifted:
                    _ensure_record_is_wizard_managed(
                        record_kind="key",
                        consumer=consumer,
                        metadata=existing_key.metadata,
                    )

                if key_value_drifted:
                    # LiteLLM's key-list payload does not prove reusable raw token material.
                    # For wizard-managed aliases, value drift means the DB record must be
                    # replaced with the generated raw key instead of adopted back into state.
                    self._api.delete_key(key_alias=consumer)
                    existing_key = self._api.create_key(
                        key=generated_key,
                        key_alias=consumer,
                        team_id=team.team_id,
                        models=expected_models,
                        metadata=managed_metadata,
                        allowed_routes=key_routes,
                    )
                    existing_keys[consumer] = existing_key
                elif key_scope_drifted:
                    # LiteLLM OSS /key/update updates settings for an existing token; use it
                    # only when the accepted raw token value already matches wizard state.
                    existing_key = self._api.update_key(
                        key_alias=consumer,
                        key=generated_key,
                        team_id=team.team_id,
                        models=expected_models,
                        metadata=managed_metadata,
                        allowed_routes=key_routes,
                    )
                    existing_keys[consumer] = existing_key
            reconciled[consumer] = existing_key
        # Retired consumers (a pack the wizard no longer provisions): sweep the
        # wizard-managed key and team so the gateway state converges too. Records
        # without the wizard marker are the operator's own and stay untouched.
        for alias, record in existing_keys.items():
            if alias in generated_keys or record.metadata.get("managed_by") != "dokploy-wizard":
                continue
            # Workspace-minted keys (the template MINTPY lane) are wizard-marked
            # but not state consumers: their alias is <login>-<workspace>. The
            # sweep must leave them alone or every real shared_core pass
            # churn-deletes the live workspace key (measured on the box).
            if record.metadata.get("coder_workspace"):
                continue
            self._api.delete_key(key_alias=alias)
        for alias, team in existing_teams.items():
            if alias in generated_keys or team.metadata.get("managed_by") != "dokploy-wizard":
                continue
            self._api.delete_team(team_id=team.team_id)
        if workspace_key_policy is not None:
            self._apply_workspace_key_policy(existing_keys, generated_keys, workspace_key_policy)
        return reconciled

    def _apply_workspace_key_policy(
        self,
        existing_keys: Mapping[str, LiteLLMVirtualKeyRecord],
        generated_keys: Mapping[str, str],
        policy: LiteLLMWorkspaceKeyPolicy,
    ) -> None:
        """Bring workspace-minted keys in line with the operator's policy.

        Workspace keys live outside the state consumer set (the template mints
        them at boot), so the retired sweep spares them and nothing else would
        re-scope one after a policy flip. The MINTPY token is deterministic —
        sha256(issuer + ":" + login + "-" + workspace) — so the master key can
        address the raw token and /key/update the grant without rebooting the
        workspace. Owners that cannot be attributed are spared: guessing a
        policy for an anonymous key could revoke a real user's access.
        """
        issuer = generated_keys.get(LITELLM_WORKSPACE_ISSUER_CONSUMER)
        if issuer is None:
            return
        for alias, record in existing_keys.items():
            if alias in generated_keys:
                continue
            if record.metadata.get("managed_by") != "dokploy-wizard":
                continue
            workspace = record.metadata.get("coder_workspace")
            owner = record.metadata.get("coder_owner")
            if not isinstance(workspace, str) or not workspace:
                continue
            if not isinstance(owner, str) or not owner:
                continue
            expected = policy.models_for_owner(owner)
            if tuple(sorted(record.models)) == tuple(sorted(expected)):
                continue
            label = f"{owner}-{workspace}"
            token = "sk-" + hashlib.sha256((issuer + ":" + label).encode()).hexdigest()[:40]
            try:
                self._api.update_key(
                    key_alias=alias,
                    key=token,
                    team_id=record.team_id,
                    models=expected,
                    metadata=record.metadata,
                )
            except LiteLLMAdminError:
                # Best-effort: an unreachable gateway or a record the update
                # route refuses simply waits for the next pass or the next
                # boot (the template lane re-mints on a model mismatch).
                continue


def _expected_models_for_consumer(
    consumer: str,
    consumer_model_allowlists: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys(consumer_model_allowlists.get(consumer, ())))


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
        token=_optional_string(payload, "token"),
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
