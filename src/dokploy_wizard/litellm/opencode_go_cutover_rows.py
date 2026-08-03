"""Per-row journal reconciliation for OpenCode Go cutovers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from typing import assert_never

from dokploy_wizard.litellm.model_admin_payload import build_owned_model_deployment
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminApi,
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
)
from dokploy_wizard.litellm.opencode_go_cutover_receipt import CutoverReceiptStore
from dokploy_wizard.litellm.opencode_go_cutover_row_intents import (
    intent_model_id,
    intent_model_name,
    inventory_row_fingerprint,
    replace_row,
    response_sha256,
    row_for,
)
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverReceipt,
    CutoverRow,
    CutoverRowOperation,
    OpenCodeGoCutoverBlockedError,
)
from dokploy_wizard.litellm.opencode_go_plan import (
    CreateIntent,
    DeleteIntent,
    NoopIntent,
    OpenCodeGoReconciliationInput,
    OpenCodeGoReconciliationIntent,
    UpdateIntent,
)
from dokploy_wizard.litellm.opencode_go_projection import (
    owner_source_id,
    record_projection,
)

_DELETE_400 = "LiteLLM model admin request failed with status 400"


class CutoverRowReconciler:
    """Write-ahead journal and exact postimage checks for each owned row."""

    def __init__(
        self,
        *,
        api: LiteLLMModelAdminApi,
        store: CutoverReceiptStore,
        clock: Callable[[], str],
        crash_hook: Callable[[str], None],
    ) -> None:
        self._api = api
        self._store = store
        self._clock = clock
        self._crash_hook = crash_hook

    def reconcile(
        self,
        receipt: CutoverReceipt,
        intents: tuple[OpenCodeGoReconciliationIntent, ...],
    ) -> CutoverReceipt:
        current = receipt
        for intent in intents:
            row = row_for(current, intent_model_id(intent))
            self._require_preimage(intent, row)
            response_sha256 = self._apply(intent)
            submitted = replace(row, status="submitted", response_sha256=response_sha256)
            current = replace_row(current, submitted, self._clock())
            self._store.write(current)
            self._crash_hook("row_submitted")
            verified = self._verify_postimage(intent, submitted)
            current = replace_row(current, verified, self._clock())
            self._store.write(current)
            self._crash_hook("row_verified")
        return replace(current, status="rows_reconciled", updated_at=self._clock())

    def post_images_match(
        self,
        receipt: CutoverReceipt,
        reconciliation_input: OpenCodeGoReconciliationInput,
    ) -> bool:
        deployments = {
            str(deployment.model_id): deployment
            for deployment in (
                build_owned_model_deployment(
                    model,
                    bootstrap_static=reconciliation_input.bootstrap_static,
                )
                for model in reconciliation_input.models
            )
        }
        owned = {
            str(record.model_id): record
            for record in self._api.list_models()
            if owner_source_id(record) is not None
        }
        expected_ids = {
            row.model_id for row in receipt.rows if row.intended_fingerprint is not None
        }
        if set(owned) != expected_ids:
            return False
        for row in receipt.rows:
            record = owned.get(row.model_id)
            if row.intended_fingerprint is None:
                if record is not None:
                    return False
                continue
            deployment = deployments.get(row.model_id)
            if deployment is None or record is None or deployment.model_name != row.model_name:
                return False
            try:
                if record_projection(record, deployment).fingerprint != row.intended_fingerprint:
                    return False
            except LiteLLMModelAdminConflict:
                return False
        return True

    def _apply(self, intent: OpenCodeGoReconciliationIntent) -> str | None:
        match intent:
            case CreateIntent(deployment=deployment):
                return self._write_model("create", deployment)
            case UpdateIntent(deployment=deployment):
                return self._write_model("update", deployment)
            case NoopIntent():
                return None
            case DeleteIntent(previous=previous):
                try:
                    self._api.delete_model(previous.model_id)
                except LiteLLMModelAdminError as error:
                    if error.reason != _DELETE_400:
                        raise
                return sha256(str(previous.model_id).encode()).hexdigest()
            case unreachable:
                assert_never(unreachable)

    def _write_model(
        self,
        operation: CutoverRowOperation,
        deployment: LiteLLMModelDeployment,
    ) -> str | None:
        try:
            match operation:
                case "create":
                    response = self._api.create_model(deployment)
                case "update":
                    response = self._api.update_model(deployment)
                case "noop" | "delete":
                    raise OpenCodeGoCutoverBlockedError("invalid model write operation")
        except LiteLLMModelAdminWriteAmbiguity:
            return None
        return response_sha256(response)

    def _require_preimage(
        self,
        intent: OpenCodeGoReconciliationIntent,
        row: CutoverRow,
    ) -> None:
        record = self._find_record(intent_model_id(intent), intent_model_name(intent))
        match intent:
            case CreateIntent():
                if record is not None:
                    raise OpenCodeGoCutoverBlockedError(
                        "create row already exists before cutover"
                    )
            case UpdateIntent(deployment=deployment) | NoopIntent(deployment=deployment):
                if (
                    record is None
                    or record_projection(record, deployment).fingerprint != row.pre_fingerprint
                ):
                    raise OpenCodeGoCutoverBlockedError("row preimage fingerprint mismatch")
            case DeleteIntent():
                if record is None or inventory_row_fingerprint(record) != row.pre_fingerprint:
                    raise OpenCodeGoCutoverBlockedError("row preimage fingerprint mismatch")
            case unreachable:
                assert_never(unreachable)

    def _verify_postimage(
        self,
        intent: OpenCodeGoReconciliationIntent,
        row: CutoverRow,
    ) -> CutoverRow:
        record = self._find_record(intent_model_id(intent), intent_model_name(intent))
        current: str | None
        match intent:
            case (
                CreateIntent(deployment=deployment)
                | UpdateIntent(deployment=deployment)
                | NoopIntent(deployment=deployment)
            ):
                if (
                    record is None
                    or record_projection(record, deployment).fingerprint
                    != row.intended_fingerprint
                ):
                    raise OpenCodeGoCutoverBlockedError("row postimage fingerprint mismatch")
                current = row.intended_fingerprint
            case DeleteIntent():
                if record is not None:
                    raise OpenCodeGoCutoverBlockedError("deleted row remains visible")
                current = None
            case unreachable:
                assert_never(unreachable)
        return replace(
            row,
            status="verified",
            current_fingerprint=current,
            verified_at=self._clock(),
        )

    def _find_record(self, model_id: str, model_name: str) -> LiteLLMModelRecord | None:
        matches = tuple(
            record
            for record in self._api.list_models()
            if str(record.model_id) == model_id or record.model_name == model_name
        )
        if len(matches) > 1:
            raise OpenCodeGoCutoverBlockedError("cutover row identity is ambiguous")
        return matches[0] if matches else None
