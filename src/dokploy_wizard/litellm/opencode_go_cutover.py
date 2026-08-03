"""Crash-safe static-to-database cutover for owned OpenCode Go aliases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import assert_never

from dokploy_wizard.litellm.model_admin_types import LiteLLMModelAdminApi
from dokploy_wizard.litellm.opencode_go_cutover_receipt import CutoverReceiptStore
from dokploy_wizard.litellm.opencode_go_cutover_row_intents import (
    aliases_for,
    rows_fingerprint,
    rows_for_intents,
)
from dokploy_wizard.litellm.opencode_go_cutover_rows import CutoverRowReconciler
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverContext,
    CutoverReceipt,
    CutoverRollback,
    CutoverVerification,
    OpenCodeGoCutoverBlockedError,
    OpenCodeGoCutoverDeployment,
    RollbackStatus,
)
from dokploy_wizard.litellm.opencode_go_plan import (
    OpenCodeGoReconciliationInput,
    build_reconciliation_plan,
    validate_reconciliation_input,
)
from dokploy_wizard.litellm.opencode_go_projection import inventory_fingerprint


class OpenCodeGoCutoverCoordinator:
    """Change the route only after every desired database row is journaled and visible."""

    def __init__(
        self,
        *,
        api: LiteLLMModelAdminApi,
        state_root: Path,
        deployment: OpenCodeGoCutoverDeployment,
        clock: Callable[[], str] | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._api = api
        self._store = CutoverReceiptStore(state_root)
        self._deployment = deployment
        self._clock = clock or _now
        self._crash_hook = crash_hook or _ignore_crash
        self._rows = CutoverRowReconciler(
            api=api,
            store=self._store,
            clock=self._clock,
            crash_hook=self._crash_hook,
        )

    def run(
        self,
        reconciliation_input: OpenCodeGoReconciliationInput,
        context: CutoverContext,
    ) -> CutoverReceipt:
        validate_reconciliation_input(reconciliation_input)
        with self._store.locked():
            receipt = self._store.load()
            if receipt is not None:
                return self._resume(receipt, reconciliation_input, context)
            return self._start(reconciliation_input, context)

    def _start(
        self,
        reconciliation_input: OpenCodeGoReconciliationInput,
        context: CutoverContext,
    ) -> CutoverReceipt:
        existing = self._api.list_models()
        plan = build_reconciliation_plan(reconciliation_input, existing)
        aliases = aliases_for(reconciliation_input)
        if context.pre_image.model_set_sha256 != _alias_fingerprint(context.static_aliases):
            raise OpenCodeGoCutoverBlockedError(
                "static preimage does not exactly inventory aliases"
            )
        if context.static_aliases != aliases:
            raise OpenCodeGoCutoverBlockedError("static preimage aliases differ from the catalog")
        if inventory_fingerprint(self._api.list_models()) != plan.inventory_fingerprint:
            raise OpenCodeGoCutoverBlockedError("LiteLLM inventory changed before cutover")
        receipt = CutoverReceipt(
            operation_id=_operation_id(context, aliases),
            owner_id=context.owner_id,
            catalog_id=context.catalog_id,
            status="intent",
            compose_id=context.compose_id,
            pre_image=context.pre_image,
            transitional_image=context.transitional_image,
            dynamic_image=context.dynamic_image,
            rows=rows_for_intents(plan.intents),
            verification=CutoverVerification(False, False, False, _alias_fingerprint(aliases)),
            rollback=CutoverRollback("not_needed", None, None, None),
            updated_at=self._clock(),
        )
        self._store.write(receipt)
        self._crash_hook("receipt_intent")
        self._deployment.deploy_transitional()
        self._deployment.verify_transitional()
        receipt = replace(
            receipt,
            status="transitional_deployed",
            verification=replace(receipt.verification, transitional_verified=True),
            updated_at=self._clock(),
        )
        self._store.write(receipt)
        self._crash_hook("transitional_deployed")
        receipt = self._rows.reconcile(receipt, plan.intents)
        if not self._rows.post_images_match(receipt, reconciliation_input):
            raise OpenCodeGoCutoverBlockedError("post-reconciliation row fingerprint mismatch")
        receipt = replace(
            receipt,
            status="visibility_verified",
            verification=replace(receipt.verification, visibility_verified=True),
            updated_at=self._clock(),
        )
        self._store.write(receipt)
        self._crash_hook("visibility_verified")
        ready = replace(receipt, status="ready_to_cutover", updated_at=self._clock())
        self._store.write(ready)
        self._crash_hook("ready_to_cutover")
        return self._switch_dynamic(ready, reconciliation_input, aliases)

    def _resume(
        self,
        receipt: CutoverReceipt,
        reconciliation_input: OpenCodeGoReconciliationInput,
        context: CutoverContext,
    ) -> CutoverReceipt:
        if (
            receipt.operation_id != _operation_id(context, context.static_aliases)
            or receipt.owner_id != context.owner_id
            or receipt.catalog_id != context.catalog_id
            or receipt.compose_id != context.compose_id
            or receipt.pre_image != context.pre_image
            or receipt.transitional_image != context.transitional_image
            or receipt.dynamic_image != context.dynamic_image
        ):
            raise OpenCodeGoCutoverBlockedError("cutover receipt identity changed")
        match receipt.status:
            case "complete":
                return receipt
            case "rollback_blocked" | "failed":
                raise OpenCodeGoCutoverBlockedError("cutover receipt is terminal")
            case (
                "intent"
                | "transitional_deployed"
                | "rows_reconciled"
                | "visibility_verified"
                | "ready_to_cutover"
                | "dynamic_deployed"
            ):
                pass
            case unreachable:
                assert_never(unreachable)
        if not self._rows.post_images_match(receipt, reconciliation_input):
            raise OpenCodeGoCutoverBlockedError("forward resume fingerprint mismatch")
        return self._switch_dynamic(
            receipt,
            reconciliation_input,
            aliases_for(reconciliation_input),
        )

    def _switch_dynamic(
        self,
        receipt: CutoverReceipt,
        reconciliation_input: OpenCodeGoReconciliationInput,
        aliases: tuple[str, ...],
    ) -> CutoverReceipt:
        try:
            self._deployment.deploy_dynamic()
        except RuntimeError as error:
            self._restore_transitional(receipt, error)
        deployed = replace(receipt, status="dynamic_deployed", updated_at=self._clock())
        self._store.write(deployed)
        self._crash_hook("dynamic_deployed")
        try:
            self._deployment.verify_dynamic(aliases)
        except RuntimeError as error:
            self._restore_transitional(deployed, error)
        if not self._rows.post_images_match(receipt, reconciliation_input):
            raise OpenCodeGoCutoverBlockedError("post-cutover visibility fingerprint mismatch")
        completed = replace(
            receipt,
            status="complete",
            verification=replace(receipt.verification, dynamic_verified=True),
            updated_at=self._clock(),
        )
        self._store.write(completed)
        self._crash_hook("complete")
        return completed

    def _restore_transitional(self, receipt: CutoverReceipt, error: RuntimeError) -> None:
        try:
            self._deployment.deploy_transitional()
            self._deployment.verify_transitional()
            rollback_status: RollbackStatus = "restored"
        except RuntimeError:
            rollback_status = "blocked"
        blocked = replace(
            receipt,
            status="rollback_blocked",
            rollback=CutoverRollback(
                rollback_status,
                receipt.pre_image.model_set_sha256,
                receipt.dynamic_image.model_set_sha256,
                rows_fingerprint(receipt.rows),
            ),
            updated_at=self._clock(),
        )
        self._store.write(blocked)
        raise OpenCodeGoCutoverBlockedError(f"dynamic deployment failed: {error}") from error


def _alias_fingerprint(aliases: tuple[str, ...]) -> str:
    return sha256("\n".join(aliases).encode()).hexdigest()


def _operation_id(context: CutoverContext, aliases: tuple[str, ...]) -> str:
    payload = f"{context.owner_id}:{context.compose_id}:{_alias_fingerprint(aliases)}"
    return sha256(payload.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ignore_crash(_: str) -> None:
    return None
