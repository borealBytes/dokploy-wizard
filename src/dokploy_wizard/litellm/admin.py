# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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
    # The gateway's user attribution (measured: /key/generate and /key/update
    # both accept `user_id`, and /key/list returns it). Workspace keys must
    # carry the person's login so spend, logs, and group grants name a human.
    user_id: str | None = None
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


# The named access group that carries the operator's full model grant.
# Measured on the deployed box (LiteLLM v1.83.14): an access group is a
# name -> registered-model-names mapping (creation rejects unregistered
# literals, wildcards included), so the wizard reconciles the group with the
# autosync-resolved sync/* names on every pass and workspace keys reference
# the GROUP rather than enumerating models. Per-person model sets later are
# one group swap on the person, not a key re-mint.
WORKSPACE_KEY_ACCESS_GROUP = "all-access"
# The gateway's UI roles for roster identities (measured on v1.83.14):
# `proxy_admin` is the admin spelling the login JWT carries; non-admin people
# are `internal_user` — spend/log views scoped, key management on their own
# surface. The per-user `permissions` object is deliberately NOT sent: the
# deployed storage adapter rejects it at user upsert (400 "Could not find
# field at upsertOneLiteLLM_UserTable.create.permissions"), so the role is
# the whole (and only API-settable) RBAC story in this build.
LITELLM_USER_ROLE_ADMIN = "proxy_admin"
LITELLM_USER_ROLE_INTERNAL = "internal_user"
# The grant a workspace-minted key (template MINTPY lane) carries when its
# owner has model access: the access group itself. The template reads the
# grant from the wizard's replacement map (single source) — test_coder_pack
# pins the wiring so the sides cannot drift apart silently.
WORKSPACE_KEY_GRANTED_MODELS: tuple[str, ...] = (WORKSPACE_KEY_ACCESS_GROUP,)
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


@dataclass(frozen=True)
class LiteLLMUserRecord:
    user_id: str
    user_email: str
    user_role: str


@dataclass(frozen=True)
class LiteLLMUserAssignment:
    """One roster person's gateway identity: login, real email, role, password."""

    user_id: str
    user_email: str
    user_role: str
    password: str


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
        user_id: str | None = None,
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
        user_id: str | None = None,
    ) -> LiteLLMVirtualKeyRecord: ...

    def list_model_names(self) -> tuple[str, ...]: ...

    def list_access_groups(self) -> tuple[str, ...]: ...

    def access_group_model_names(self, *, name: str) -> tuple[str, ...] | None: ...

    def create_access_group(self, *, name: str, model_names: tuple[str, ...]) -> None: ...

    def update_access_group(self, *, name: str, model_names: tuple[str, ...]) -> None: ...

    def list_users(self) -> tuple[LiteLLMUserRecord, ...]: ...

    def upsert_user(
        self, *, user_id: str, user_email: str, user_role: str, password: str
    ) -> None: ...

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
        user_id: str | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        body: dict[str, object] = {
            "key": key,
            "key_alias": key_alias,
            "team_id": team_id,
            "models": list(models),
            "metadata": dict(metadata or {}),
        }
        if user_id is not None:
            body["user_id"] = user_id
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
        user_id: str | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        body: dict[str, object] = {
            "key_alias": key_alias,
            "key": key,
            "team_id": team_id,
            "models": list(models),
            "metadata": dict(metadata or {}),
        }
        if user_id is not None:
            body["user_id"] = user_id
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

    def list_model_names(self) -> tuple[str, ...]:
        # /model/info answers {"data": [{model_name, ...}, ...]} (measured);
        # the group reconcile needs the registry's real names — access-group
        # creation rejects literals it does not serve, wildcards included.
        payload = self._request_json("GET", "/model/info")
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise LiteLLMAdminError("LiteLLM model.info response must carry a data list.")
        names = {
            str(row.get("model_name"))
            for row in rows
            if isinstance(row, dict) and row.get("model_name")
        }
        return tuple(sorted(names))

    def list_access_groups(self) -> tuple[str, ...]:
        payload = self._request_json("GET", "/access_group/list")
        if not isinstance(payload, dict):
            raise LiteLLMAdminError("LiteLLM access_group.list response must be an object.")
        groups = payload.get("access_groups")
        if not isinstance(groups, list):
            return ()
        names: list[str] = []
        for item in groups:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("access_group") or item.get("name")
                if name:
                    names.append(str(name))
        return tuple(sorted(names))

    def access_group_model_names(self, *, name: str) -> tuple[str, ...] | None:
        payload = self._request_json("GET", f"/access_group/{parse.quote(name, safe='')}/info")
        record = payload.get("info") if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            record = payload if isinstance(payload, dict) else None
        if record is None:
            return None
        models = record.get("model_names") or record.get("models")
        return _tuple_of_strings(models)

    def create_access_group(self, *, name: str, model_names: tuple[str, ...]) -> None:
        self._request_json(
            "POST",
            "/access_group/new",
            {"access_group": name, "model_names": list(model_names)},
        )

    def update_access_group(self, *, name: str, model_names: tuple[str, ...]) -> None:
        self._request_json(
            "PUT",
            f"/access_group/{parse.quote(name, safe='')}/update",
            {"model_names": list(model_names)},
        )

    def list_users(self) -> tuple[LiteLLMUserRecord, ...]:
        # Measured: /user/list answers {"users": [...]} with the raw user rows
        # (user_id, user_email, user_role). No pagination flags were needed at
        # box scale; the parse stays defensive on shape, not on volume.
        payload = self._request_json("GET", "/user/list")
        rows = payload.get("users") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise LiteLLMAdminError("LiteLLM user.list response must carry a users list.")
        records: list[LiteLLMUserRecord] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("user_id"):
                continue
            records.append(
                LiteLLMUserRecord(
                    user_id=str(row["user_id"]),
                    user_email=str(row.get("user_email") or ""),
                    user_role=str(row.get("user_role") or ""),
                )
            )
        return tuple(records)

    def upsert_user(
        self, *, user_id: str, user_email: str, user_role: str, password: str
    ) -> None:
        # /user/update is THE identity upsert (measured): an unknown user_id
        # creates the row without auto-minting a key, and a password set here
        # is what makes /v2/login work (the auto-mint key of /user/new is a
        # side effect this route does not have).
        self._request_json(
            "POST",
            "/user/update",
            {
                "user_id": user_id,
                "user_email": user_email,
                "user_role": user_role,
                "password": password,
            },
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

    def ensure_workspace_access_group(
        self, *, group: str = WORKSPACE_KEY_ACCESS_GROUP
    ) -> tuple[str, ...]:
        """Converge the named access group onto the gateway's sync-served models.

        Measured on the deployed build (v1.83.14): group creation accepts only
        registered model names (wildcard literals are rejected), so the wizard
        resolves the sync/* set from /model/info and PUTs the current names on
        every pass. Autosync adding a model therefore re-grants it to every
        workspace key without touching the keys themselves — and a future
        per-person grant is one group swap on the person, not a re-mint.
        """
        served = self._api.list_model_names()
        expected = tuple(sorted(name for name in served if name.startswith("sync/")))
        if not expected:
            # Model sync has not registered anything yet (cold gateway, or sync
            # disabled for lack of upstream creds). An empty group cannot be
            # created (measured: creation resolves every literal against the
            # registry), so simply wait — the next pass converges it, and no
            # key needs the group until a sync model exists to serve.
            return ()
        groups = self._api.list_access_groups()
        if group not in groups:
            self._api.create_access_group(name=group, model_names=expected)
            return expected
        current = self._api.access_group_model_names(name=group)
        if current is not None and tuple(sorted(current)) != expected:
            self._api.update_access_group(name=group, model_names=expected)
        return expected

    def ensure_user_identities(self, assignments: Sequence[LiteLLMUserAssignment]) -> list[str]:
        """Stand every roster person up as a gateway user, before first boot.

        One ID across surfaces means the LiteLLM row exists the moment the
        Coder roster gains a person: workspace keys can attribute to a real
        user immediately, and the person can sign into the gateway UI (the
        password rides with the create; /v2/login by their email). Existing
        rows converge on the roster's email and role; the password is only
        re-synced when something else drifts, so a person who rotated their
        gateway password in the UI keeps it on otherwise-quiet passes. The
        deployment's admin login alone rides the admin role.
        """
        existing = {record.user_id: record for record in self._api.list_users()}
        notes: list[str] = []
        for item in sorted(assignments, key=lambda assignment: assignment.user_id):
            current = existing.get(item.user_id)
            if (
                current is not None
                and current.user_email == item.user_email
                and current.user_role == item.user_role
            ):
                continue
            self._api.upsert_user(
                user_id=item.user_id,
                user_email=item.user_email,
                user_role=item.user_role,
                password=item.password,
            )
            verb = "Updated" if current is not None else "Created"
            notes.append(f"{verb} LiteLLM user '{item.user_id}' ({item.user_role}).")
        return notes

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
        workspace. The same update carries the identity-identity migrations:
        the `coder-<login>-<workspace>` alias and the owner's user_id (a key
        minted before person attribution predates it and is renamed in place).
        Owners that cannot be attributed are spared: guessing a
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
            expected_models = policy.models_for_owner(owner)
            expected_alias = f"coder-{owner}-{workspace}"
            drifted = (
                tuple(sorted(record.models)) != tuple(sorted(expected_models))
                or alias != expected_alias
                or record.user_id != owner
            )
            if not drifted:
                continue
            label = f"{owner}-{workspace}"
            token = "sk-" + hashlib.sha256((issuer + ":" + label).encode()).hexdigest()[:40]
            try:
                self._api.update_key(
                    key_alias=expected_alias,
                    key=token,
                    team_id=record.team_id,
                    models=expected_models,
                    metadata=record.metadata,
                    user_id=owner,
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
        user_id=_optional_string(payload, "user_id"),
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
