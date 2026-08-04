from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias, assert_never

from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminApi,
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
    LiteLLMModelRecord,
    ModelUuid,
)
from dokploy_wizard.litellm.opencode_go_plan import (
    CreateIntent,
    DeleteIntent,
    NoopIntent,
    OpenCodeGoReconciliationInput,
    OpenCodeGoReconciliationIntent,
    OpenCodeGoReconciliationPlan,
    UpdateIntent,
    build_reconciliation_plan,
    validate_reconciliation_input,
)
from dokploy_wizard.litellm.opencode_go_projection import (
    inventory_fingerprint,
    owner_source_id,
    record_projection,
)

_DELETE_400 = "LiteLLM model admin request failed with status 400"
WriteIntent: TypeAlias = CreateIntent | UpdateIntent


@dataclass(frozen=True, slots=True)
class OpenCodeGoAppliedIntent:
    kind: Literal["create", "update", "noop", "delete"]
    model_name: str
    model_id: ModelUuid
    record: LiteLLMModelRecord | None


@dataclass(frozen=True, slots=True)
class OpenCodeGoReconciliationResult:
    plan: OpenCodeGoReconciliationPlan
    applied: tuple[OpenCodeGoAppliedIntent, ...]


class OpenCodeGoDatabaseReconciler:
    def __init__(self, api: LiteLLMModelAdminApi) -> None:
        self._api = api

    def reconcile(
        self, reconciliation_input: OpenCodeGoReconciliationInput
    ) -> OpenCodeGoReconciliationResult:
        validate_reconciliation_input(reconciliation_input)
        existing = self._api.list_models()
        plan = build_reconciliation_plan(reconciliation_input, existing)
        visible = self._api.list_models()
        if inventory_fingerprint(visible) != plan.inventory_fingerprint:
            raise LiteLLMModelAdminConflict("LiteLLM inventory visibility changed before mutation")
        return OpenCodeGoReconciliationResult(
            plan=plan,
            applied=tuple(self._apply(intent) for intent in plan.intents),
        )

    def _apply(self, intent: OpenCodeGoReconciliationIntent) -> OpenCodeGoAppliedIntent:
        match intent:
            case CreateIntent() as write_intent:
                record = self._create(write_intent)
                return OpenCodeGoAppliedIntent(
                    "create",
                    write_intent.deployment.model_name,
                    write_intent.deployment.model_id,
                    record,
                )
            case UpdateIntent() as write_intent:
                record = self._update(write_intent)
                return OpenCodeGoAppliedIntent(
                    "update",
                    write_intent.deployment.model_name,
                    write_intent.deployment.model_id,
                    record,
                )
            case NoopIntent(deployment=deployment, previous=previous):
                return OpenCodeGoAppliedIntent(
                    "noop", deployment.model_name, deployment.model_id, previous
                )
            case DeleteIntent(previous=previous):
                self._delete(previous)
                return OpenCodeGoAppliedIntent(
                    "delete", previous.model_name, previous.model_id, None
                )
            case unreachable:
                assert_never(unreachable)

    def _create(self, intent: CreateIntent) -> LiteLLMModelRecord:
        try:
            record = self._api.create_model(intent.deployment)
        except LiteLLMModelAdminWriteAmbiguity as exc:
            return self._recover_lost_write(intent, exc)
        self._require_exact(record, intent, "create response")
        return record

    def _update(self, intent: UpdateIntent) -> LiteLLMModelRecord:
        try:
            record = self._api.update_model(intent.deployment)
        except LiteLLMModelAdminWriteAmbiguity as exc:
            return self._recover_lost_write(intent, exc)
        self._require_exact(record, intent, "patch response")
        return record

    def _recover_lost_write(
        self,
        intent: WriteIntent,
        cause: LiteLLMModelAdminWriteAmbiguity,
    ) -> LiteLLMModelRecord:
        operation = _write_operation(intent)
        matches = tuple(
            record
            for record in self._api.list_models()
            if record.model_name == intent.deployment.model_name
        )
        if not matches:
            raise LiteLLMModelAdminConflict(
                f"lost {operation} response left no owned alias"
            ) from cause
        if len(matches) > 1:
            raise LiteLLMModelAdminConflict(
                f"lost {operation} response left multiple owned aliases"
            ) from cause
        try:
            self._require_exact(
                matches[0], intent, f"lost {operation} response"
            )
        except LiteLLMModelAdminConflict as exc:
            raise LiteLLMModelAdminConflict(
                f"lost {operation} response mismatch for {intent.deployment.model_name}"
            ) from exc
        return matches[0]

    def _delete(self, previous: LiteLLMModelRecord) -> None:
        try:
            self._api.delete_model(previous.model_id)
        except LiteLLMModelAdminError as exc:
            if exc.reason != _DELETE_400:
                raise
            surviving = tuple(
                record
                for record in self._api.list_models()
                if record.model_name == previous.model_name or record.model_id == previous.model_id
            )
            if surviving:
                raise LiteLLMModelAdminConflict(
                    f"delete 400 recovery found surviving alias {previous.model_name}"
                ) from exc

    def _require_exact(
        self,
        record: LiteLLMModelRecord,
        intent: WriteIntent,
        context: str,
    ) -> None:
        if owner_source_id(record) != intent.deployment.model_info.source_id:
            raise LiteLLMModelAdminConflict(f"{context} is not an owned OpenCode Go alias")
        actual = record_projection(record, intent.deployment).fingerprint
        if actual != intent.expected_fingerprint:
            raise LiteLLMModelAdminConflict(f"{context} owned projection mismatch")


def _write_operation(intent: WriteIntent) -> Literal["create", "patch"]:
    match intent:
        case CreateIntent():
            return "create"
        case UpdateIntent():
            return "patch"
        case unreachable:
            assert_never(unreachable)
