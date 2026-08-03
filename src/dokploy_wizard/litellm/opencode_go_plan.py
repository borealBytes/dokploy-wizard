from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, TypeAlias, assert_never

from dokploy_wizard.litellm.catalog_json import canonical_json_bytes, sha256_bytes
from dokploy_wizard.litellm.catalog_observation_types import CatalogModel
from dokploy_wizard.litellm.catalog_state_types import CatalogState
from dokploy_wizard.litellm.model_admin_payload import build_owned_model_deployment
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
)
from dokploy_wizard.litellm.opencode_go_projection import (
    deployment_projection,
    inventory_fingerprint,
    owner_source_id,
    record_projection,
)


@dataclass(frozen=True, slots=True)
class OpenCodeGoReconciliationInput:
    models: tuple[CatalogModel, ...]
    catalog_state: CatalogState
    bootstrap_static: bool


@dataclass(frozen=True, slots=True)
class CreateIntent:
    deployment: LiteLLMModelDeployment
    expected_fingerprint: str
    kind: Literal["create"] = "create"


@dataclass(frozen=True, slots=True)
class UpdateIntent:
    deployment: LiteLLMModelDeployment
    previous: LiteLLMModelRecord
    expected_fingerprint: str
    kind: Literal["update"] = "update"


@dataclass(frozen=True, slots=True)
class NoopIntent:
    deployment: LiteLLMModelDeployment
    previous: LiteLLMModelRecord
    expected_fingerprint: str
    kind: Literal["noop"] = "noop"


@dataclass(frozen=True, slots=True)
class DeleteIntent:
    previous: LiteLLMModelRecord
    kind: Literal["delete"] = "delete"


OpenCodeGoReconciliationIntent: TypeAlias = CreateIntent | UpdateIntent | NoopIntent | DeleteIntent


@dataclass(frozen=True, slots=True)
class OpenCodeGoReconciliationPlan:
    inventory_fingerprint: str
    intents: tuple[OpenCodeGoReconciliationIntent, ...]


def build_reconciliation_plan(
    reconciliation_input: OpenCodeGoReconciliationInput,
    records: tuple[LiteLLMModelRecord, ...],
) -> OpenCodeGoReconciliationPlan:
    validate_reconciliation_input(reconciliation_input)
    deployments = _deployments(reconciliation_input)
    records_by_name = _records_by_name(records)
    stale_sources = _eligible_stale_sources(reconciliation_input)
    _validate_owner_proof(records_by_name)
    intents: list[OpenCodeGoReconciliationIntent] = []
    desired_sources: set[str] = set()
    for deployment in deployments:
        desired_sources.add(deployment.model_info.source_id)
        previous = records_by_name.get(deployment.model_name)
        expected = deployment_projection(deployment).fingerprint
        if previous is None:
            intents.append(CreateIntent(deployment, expected))
            continue
        source_id = owner_source_id(previous)
        if source_id != deployment.model_info.source_id:
            raise LiteLLMModelAdminConflict(
                f"source mismatch for {deployment.model_name}"
            )
        actual = record_projection(previous, deployment).fingerprint
        if actual == expected:
            intents.append(NoopIntent(deployment, previous, expected))
            continue
        preserved = replace(
            deployment,
            server_model_info_extras=tuple(sorted(previous.server_model_info_extras.items())),
        )
        intents.append(UpdateIntent(preserved, previous, expected))
    for record in records_by_name.values():
        source_id = owner_source_id(record)
        should_delete = (
            source_id is not None
            and source_id not in desired_sources
            and source_id in stale_sources
        )
        if should_delete:
            intents.append(DeleteIntent(record))
    return OpenCodeGoReconciliationPlan(
        inventory_fingerprint=inventory_fingerprint(records),
        intents=tuple(sorted(intents, key=_intent_sort_key)),
    )


def validate_reconciliation_input(reconciliation_input: OpenCodeGoReconciliationInput) -> None:
    catalog_state = reconciliation_input.catalog_state
    match catalog_state.state:
        case "enabled":
            pass
        case "disabled" | "blocked":
            raise LiteLLMModelAdminConflict("OpenCode Go catalog is not enabled")
        case state_unreachable:
            assert_never(state_unreachable)
    observation = catalog_state.observation
    if observation is None or not observation.complete or not observation.accepted_ids:
        raise LiteLLMModelAdminConflict("catalog visibility failure")
    match observation.status:
        case "accepted" | "accepted_with_quarantine":
            pass
        case "rejected_invalid" | "rejected_clock_regression" | "quarantined_anomalous":
            raise LiteLLMModelAdminConflict("catalog visibility failure")
        case status_unreachable:
            assert_never(status_unreachable)
    match catalog_state.anomaly.state:
        case "none" | "confirmed" | "cleared":
            pass
        case "pending" | "quarantined":
            raise LiteLLMModelAdminConflict(
                "catalog anomalous shrink blocks reconciliation"
            )
        case anomaly_unreachable:
            assert_never(anomaly_unreachable)
    source_ids = tuple(model.source_id for model in reconciliation_input.models)
    if source_ids != tuple(sorted(set(source_ids))) or source_ids != observation.accepted_ids:
        raise LiteLLMModelAdminConflict("catalog source drift blocks reconciliation")
    expected_sha = sha256_bytes(canonical_json_bytes(list(observation.source_ids)))
    if observation.source_ids_sha256 != expected_sha:
        raise LiteLLMModelAdminConflict("catalog visibility failure")


def _deployments(
    reconciliation_input: OpenCodeGoReconciliationInput,
) -> tuple[LiteLLMModelDeployment, ...]:
    try:
        return tuple(
            build_owned_model_deployment(
                model, bootstrap_static=reconciliation_input.bootstrap_static
            )
            for model in reconciliation_input.models
        )
    except LiteLLMModelAdminError as exc:
        raise LiteLLMModelAdminConflict(f"missing pricing metadata: {exc.reason}") from exc


def _eligible_stale_sources(reconciliation_input: OpenCodeGoReconciliationInput) -> frozenset[str]:
    observation = reconciliation_input.catalog_state.observation
    assert observation is not None
    visible_sha = sha256_bytes(canonical_json_bytes(list(observation.source_ids)))
    stale_sources: set[str] = set()
    for record in reconciliation_input.catalog_state.missing:
        match record.state:
            case "eligible_for_delete":
                if record.observation_sha256 != visible_sha:
                    raise LiteLLMModelAdminConflict("stale row visibility failure")
                stale_sources.add(record.source_id)
            case "present" | "pending_absence" | "confirmed_absence" | "deleted" | "reappeared":
                continue
            case unreachable:
                assert_never(unreachable)
    return frozenset(stale_sources)


def _records_by_name(
    records: tuple[LiteLLMModelRecord, ...],
) -> dict[str, LiteLLMModelRecord]:
    by_name: dict[str, LiteLLMModelRecord] = {}
    for record in records:
        if record.model_name in by_name:
            raise LiteLLMModelAdminConflict(
                f"duplicate LiteLLM deployment alias {record.model_name}"
            )
        by_name[record.model_name] = record
    return by_name


def _validate_owner_proof(records_by_name: dict[str, LiteLLMModelRecord]) -> None:
    for model_name, record in records_by_name.items():
        if not model_name.startswith("opencode-go/"):
            continue
        source_id = owner_source_id(record)
        if source_id is None:
            nested_source_id = record.model_info.get("source_id")
            if (
                isinstance(nested_source_id, str)
                and model_name != f"opencode-go/{nested_source_id}"
            ):
                raise LiteLLMModelAdminConflict(f"source mismatch for {model_name}")
            raise LiteLLMModelAdminConflict(f"unowned alias {model_name}")
        if model_name != f"opencode-go/{source_id}":
            raise LiteLLMModelAdminConflict(f"source mismatch for {model_name}")


def _intent_sort_key(intent: OpenCodeGoReconciliationIntent) -> tuple[int, str]:
    match intent:
        case CreateIntent(deployment=deployment):
            return (0, deployment.model_name)
        case UpdateIntent(deployment=deployment):
            return (1, deployment.model_name)
        case NoopIntent(deployment=deployment):
            return (2, deployment.model_name)
        case DeleteIntent(previous=previous):
            return (3, previous.model_name)
        case unreachable:
            assert_never(unreachable)
