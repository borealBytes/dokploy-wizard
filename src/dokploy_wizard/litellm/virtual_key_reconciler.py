"""Generic managed LiteLLM team and virtual-key reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeVar

from dokploy_wizard.litellm.admin import (
    LiteLLMAdminApi,
    LiteLLMAdminError,
    LiteLLMTeamRecord,
    LiteLLMVirtualKeyRecord,
)

MetadataValue = TypeVar("MetadataValue")


@dataclass(frozen=True, slots=True)
class VirtualKeyInventory:
    teams: tuple[LiteLLMTeamRecord, ...]
    keys: tuple[LiteLLMVirtualKeyRecord, ...]

    def is_empty(self) -> bool:
        return not self.teams and not self.keys


@dataclass(frozen=True, slots=True)
class ManagedKeyReconciliationRequest:
    generated_keys: Mapping[str, str]
    consumer_model_allowlists: Mapping[str, tuple[str, ...]]
    consumers: tuple[str, ...]
    inventory: VirtualKeyInventory


def read_virtual_key_inventory(api: LiteLLMAdminApi) -> VirtualKeyInventory:
    return VirtualKeyInventory(teams=api.list_teams(), keys=api.list_keys())


def expected_models_for_consumer(
    consumer: str,
    consumer_model_allowlists: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys(consumer_model_allowlists.get(consumer, ())))


def reconcile_managed_virtual_keys(
    api: LiteLLMAdminApi,
    reconciliation: ManagedKeyReconciliationRequest,
) -> dict[str, LiteLLMVirtualKeyRecord]:
    existing_teams = {team.team_alias: team for team in reconciliation.inventory.teams}
    existing_keys = {record.key_alias: record for record in reconciliation.inventory.keys}
    reconciled: dict[str, LiteLLMVirtualKeyRecord] = {}
    for consumer in reconciliation.consumers:
        generated_key = reconciliation.generated_keys[consumer]
        expected_models = expected_models_for_consumer(
            consumer,
            reconciliation.consumer_model_allowlists,
        )
        managed_metadata = _managed_metadata_for_consumer(consumer)
        team = existing_teams.get(consumer)
        if team is None:
            team = api.create_team(
                team_alias=consumer,
                models=expected_models,
                metadata=managed_metadata,
            )
            existing_teams[consumer] = team
        elif team.models != expected_models:
            _ensure_record_is_wizard_managed("team", consumer, team.metadata)
            team = api.update_team(
                team_id=team.team_id,
                team_alias=consumer,
                models=expected_models,
                metadata=managed_metadata,
            )
            existing_teams[consumer] = team

        existing_key = existing_keys.get(consumer)
        if existing_key is None:
            existing_key = api.create_key(
                key=generated_key,
                key_alias=consumer,
                team_id=team.team_id,
                models=expected_models,
                metadata=managed_metadata,
            )
            existing_keys[consumer] = existing_key
        else:
            key_value_drifted = existing_key.key != generated_key
            key_scope_drifted = (
                existing_key.models != expected_models or existing_key.team_id != team.team_id
            )
            if key_value_drifted or key_scope_drifted:
                _ensure_record_is_wizard_managed("key", consumer, existing_key.metadata)
            if key_value_drifted:
                api.delete_key(key_alias=consumer)
                existing_key = api.create_key(
                    key=generated_key,
                    key_alias=consumer,
                    team_id=team.team_id,
                    models=expected_models,
                    metadata=managed_metadata,
                )
                existing_keys[consumer] = existing_key
            elif key_scope_drifted:
                existing_key = api.update_key(
                    key_alias=consumer,
                    key=generated_key,
                    team_id=team.team_id,
                    models=expected_models,
                    metadata=managed_metadata,
                )
                existing_keys[consumer] = existing_key
        reconciled[consumer] = existing_key
    return reconciled


def _managed_metadata_for_consumer(consumer: str) -> dict[str, str]:
    return {"consumer": consumer, "managed_by": "dokploy-wizard"}


def _ensure_record_is_wizard_managed(
    record_kind: str,
    consumer: str,
    metadata: Mapping[str, MetadataValue],
) -> None:
    if metadata.get("managed_by") != "dokploy-wizard":
        raise LiteLLMAdminError(
            f"LiteLLM {record_kind} '{consumer}' drifted but is not wizard-managed. "
            "Refusing to mutate it silently."
        )
    metadata_consumer = metadata.get("consumer")
    if metadata_consumer != consumer:
        raise LiteLLMAdminError(
            f"LiteLLM {record_kind} '{consumer}' drifted but metadata belongs to "
            f"consumer {metadata_consumer!r}. Refusing to mutate it silently."
        )
