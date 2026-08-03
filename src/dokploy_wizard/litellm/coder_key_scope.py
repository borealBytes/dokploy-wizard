"""Fail-closed scope reconciliation for the two existing Coder virtual keys."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Final, Literal, TypeAlias, TypeVar

from dokploy_wizard.litellm.admin import (
    LiteLLMAdminApi,
    LiteLLMAdminError,
    LiteLLMTeamRecord,
    LiteLLMVirtualKeyRecord,
)

CoderConsumer: TypeAlias = Literal["coder-hermes", "coder-kdense"]
ScopeFailureStatus: TypeAlias = Literal["blocked", "failed"]
ScopePhase: TypeAlias = Literal["request", "inventory", "identity", "mutation", "postcheck"]
TeamSnapshot: TypeAlias = tuple[str, str, tuple[str, ...], bytes]
KeySnapshot: TypeAlias = tuple[str, str, str | None, tuple[str, ...], bytes]
FailureContext: TypeAlias = tuple[CoderConsumer | None, tuple[str, ...]]
MetadataValue = TypeVar("MetadataValue")

CODER_CONSUMERS: Final[tuple[CoderConsumer, ...]] = (
    "coder-hermes",
    "coder-kdense",
)
ALL_PROXY_MODELS: Final[tuple[str, ...]] = ("all-proxy-models",)


class CoderScopeReconciliationError(LiteLLMAdminError):
    """Mutable value-free exception so Python can attach traceback state."""

    def __init__(
        self,
        *,
        status: ScopeFailureStatus,
        phase: ScopePhase,
        context: FailureContext = (None, ()),
    ) -> None:
        super().__init__()
        self.status = status
        self.phase = phase
        self.consumer, self.completed_mutations = context

    def __str__(self) -> str:
        target = "" if self.consumer is None else f" for {self.consumer}"
        return f"LiteLLM Coder scope reconciliation {self.status} during {self.phase}{target}."


@dataclass(frozen=True, slots=True)
class CoderScopeReconciliationResult:
    """Successful reconciliation result without cleartext virtual-key values."""

    status: Literal["completed"]
    completed_mutations: tuple[str, ...]
    key_records: tuple[LiteLLMVirtualKeyRecord, ...]


@dataclass(frozen=True, slots=True)
class _Inventory:
    teams: tuple[LiteLLMTeamRecord, ...]
    keys: tuple[LiteLLMVirtualKeyRecord, ...]


@dataclass(frozen=True, slots=True)
class _InventorySnapshot:
    teams: tuple[TeamSnapshot, ...]
    keys: tuple[KeySnapshot, ...]


@dataclass(frozen=True, slots=True)
class _CoderTarget:
    consumer: CoderConsumer
    raw_key: str
    team: LiteLLMTeamRecord
    key: LiteLLMVirtualKeyRecord


def reconcile_existing_coder_key_scopes(
    api: LiteLLMAdminApi,
    state_virtual_keys: Mapping[str, str],
    requested_scopes: Mapping[str, tuple[str, ...]],
) -> CoderScopeReconciliationResult:
    """Set exact all-proxy scopes only after both state-backed identities are proven."""

    require_exact_coder_scope_request(state_virtual_keys, requested_scopes)
    before = _read_inventory(api, completed_mutations=())
    targets = tuple(
        _target_for(before, consumer, state_virtual_keys[consumer])
        for consumer in CODER_CONSUMERS
    )
    expected_after = _expected_snapshot(before)
    completed_mutations: list[str] = []

    for target in targets:
        if target.team.models != ALL_PROXY_MODELS:
            _update_team(api, target, completed_mutations)
        if target.key.models != ALL_PROXY_MODELS:
            _update_key(api, target, completed_mutations)

    after = _read_inventory(api, completed_mutations=tuple(completed_mutations))
    if _snapshot(after) != expected_after:
        raise CoderScopeReconciliationError(
            status="blocked",
            phase="postcheck",
            context=(None, tuple(completed_mutations)),
        )
    return CoderScopeReconciliationResult(
        status="completed",
        completed_mutations=tuple(completed_mutations),
        key_records=after.keys,
    )


def require_exact_coder_scope_request(
    state_virtual_keys: Mapping[str, str],
    requested_scopes: Mapping[str, tuple[str, ...]],
) -> None:
    for consumer in CODER_CONSUMERS:
        raw_key = state_virtual_keys.get(consumer)
        if raw_key is None or raw_key == "":
            raise CoderScopeReconciliationError(
                status="blocked",
                phase="request",
                context=(consumer, ()),
            )
        if requested_scopes.get(consumer) != ALL_PROXY_MODELS:
            raise CoderScopeReconciliationError(
                status="blocked",
                phase="request",
                context=(consumer, ()),
            )


def _read_inventory(
    api: LiteLLMAdminApi,
    *,
    completed_mutations: tuple[str, ...],
) -> _Inventory:
    try:
        return _Inventory(teams=api.list_teams(), keys=api.list_keys())
    except LiteLLMAdminError:
        raise CoderScopeReconciliationError(
            status="failed",
            phase="inventory",
            context=(None, completed_mutations),
        ) from None


def _target_for(
    inventory: _Inventory,
    consumer: CoderConsumer,
    raw_key: str,
) -> _CoderTarget:
    teams = tuple(team for team in inventory.teams if team.team_alias == consumer)
    keys = tuple(key for key in inventory.keys if key.key_alias == consumer)
    if len(teams) != 1 or len(keys) != 1:
        raise CoderScopeReconciliationError(
            status="blocked",
            phase="identity",
            context=(consumer, ()),
        )
    team = teams[0]
    key = keys[0]
    identity_matches = (
        key.key == sha256(raw_key.encode()).hexdigest()
        and key.team_id == team.team_id
        and key.metadata.get("managed_by") == "dokploy-wizard"
        and key.metadata.get("consumer") == consumer
        and team.metadata.get("managed_by") == "dokploy-wizard"
        and team.metadata.get("consumer") == consumer
    )
    if not identity_matches:
        raise CoderScopeReconciliationError(
            status="blocked",
            phase="identity",
            context=(consumer, ()),
        )
    return _CoderTarget(consumer=consumer, raw_key=raw_key, team=team, key=key)


def _update_team(
    api: LiteLLMAdminApi,
    target: _CoderTarget,
    completed_mutations: list[str],
) -> None:
    try:
        api.update_team(
            team_id=target.team.team_id,
            team_alias=target.team.team_alias,
            models=ALL_PROXY_MODELS,
            metadata=target.team.metadata,
        )
    except LiteLLMAdminError:
        raise CoderScopeReconciliationError(
            status="failed",
            phase="mutation",
            context=(target.consumer, tuple(completed_mutations)),
        ) from None
    completed_mutations.append(f"team:{target.consumer}")


def _update_key(
    api: LiteLLMAdminApi,
    target: _CoderTarget,
    completed_mutations: list[str],
) -> None:
    try:
        api.update_key(
            key_alias=target.key.key_alias,
            key=target.raw_key,
            team_id=target.key.team_id,
            models=ALL_PROXY_MODELS,
            metadata=target.key.metadata,
        )
    except LiteLLMAdminError:
        raise CoderScopeReconciliationError(
            status="failed",
            phase="mutation",
            context=(target.consumer, tuple(completed_mutations)),
        ) from None
    completed_mutations.append(f"key:{target.consumer}")


def _expected_snapshot(inventory: _Inventory) -> _InventorySnapshot:
    teams = tuple(
        replace(team, models=ALL_PROXY_MODELS)
        if team.team_alias in CODER_CONSUMERS
        else team
        for team in inventory.teams
    )
    keys = tuple(
        replace(key, models=ALL_PROXY_MODELS)
        if key.key_alias in CODER_CONSUMERS
        else key
        for key in inventory.keys
    )
    return _snapshot(_Inventory(teams=teams, keys=keys))


def _snapshot(inventory: _Inventory) -> _InventorySnapshot:
    return _InventorySnapshot(
        teams=tuple(
            sorted(
                (
                    (
                        team.team_id,
                        team.team_alias,
                        team.models,
                        _metadata_bytes(team.metadata),
                    )
                    for team in inventory.teams
                ),
                key=lambda team: (team[1], team[0]),
            )
        ),
        keys=tuple(
            sorted(
                (
                    (
                        key.key,
                        key.key_alias,
                        key.team_id,
                        key.models,
                        _metadata_bytes(key.metadata),
                    )
                    for key in inventory.keys
                ),
                key=lambda key: (key[1], key[0]),
            )
        ),
    )


def _metadata_bytes(metadata: Mapping[str, MetadataValue]) -> bytes:
    return json.dumps(
        dict(metadata),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
