from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import NoReturn, assert_never

from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptError,
    CoderSecretReceiptStep,
    CoderSecretReceiptStore,
    SecretOperation,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation_types import (
    CoderSecretClient,
    CoderSecretError,
    CoderSecretMetadata,
    CoderSecretSpec,
    MetadataPair,
    current_step,
    metadata_hash,
    metadata_matches,
    new_intent,
    now,
    operation_for_metadata,
    replace_step,
    valid_hash,
    validate_receipt_for_specs,
    value_hash,
)
from dokploy_wizard.dokploy.task1_coder_secret_attestation import (
    Task1CoderSecretAttestationError,
    proves_task1_created_secret,
)


class CoderSecretReconciler:
    def __init__(self, *, state_dir: Path, client: CoderSecretClient, owner_id: str) -> None:
        self._store = CoderSecretReceiptStore(state_dir)
        self._client = client
        self._owner_id = owner_id

    def reconcile(self, specs: tuple[CoderSecretSpec, ...]) -> CoderSecretReceipt:
        if not specs or len({spec.name for spec in specs}) != len(specs):
            raise CoderSecretError(
                "Coder secret reconciliation names are invalid",
                kind="reconciliation",
            )
        try:
            existing = self._store.load()
        except CoderSecretReceiptError as error:
            raise CoderSecretError(
                "Coder secret receipt cannot be read",
                kind="receipt_invalid",
            ) from error
        if existing is not None:
            if existing.owner_id != self._owner_id:
                raise CoderSecretError(
                    "Coder secret receipt ownership does not match",
                    kind="receipt_invalid",
                )
            validate_receipt_for_specs(existing, specs)
        receipt = existing or CoderSecretReceipt(self._owner_id, "planned", ())
        if receipt.status in {"blocked", "failed"}:
            raise CoderSecretError("Coder secret receipt is terminal", kind="receipt")
        for spec in sorted(specs, key=lambda item: item.name):
            receipt = self._reconcile_one(receipt, spec)
        completed = replace(receipt, status="completed")
        self._write(completed)
        return completed

    def _reconcile_one(
        self, receipt: CoderSecretReceipt, spec: CoderSecretSpec
    ) -> CoderSecretReceipt:
        pair = self._metadata_pair(spec.name)
        previous = current_step(receipt, spec.name)
        if not pair.stable:
            operation: SecretOperation = "create" if pair.first is None else "update"
            self._block(receipt, new_intent(spec, operation, pair), "metadata drift")
        if previous is not None:
            match previous.status:
                case "intent":
                    return self._recover_intent(receipt, previous, spec, pair)
                case "submitted":
                    return self._verify_submitted(receipt, previous, spec)
                case "verified":
                    if previous.source_value_sha256 == value_hash(spec.value):
                        self._require_current_metadata(receipt, previous, spec, pair.first)
                        return receipt
                    self._require_current_metadata(receipt, previous, spec, pair.first)
                case "blocked":
                    self._block(receipt, previous, "blocked receipt")
                case _ as unreachable_step_status:
                    assert_never(unreachable_step_status)
        operation = operation_for_metadata(previous, pair.first)
        intent = new_intent(spec, operation, pair)
        if pair.first is not None and not self._is_owned(previous, pair.first, spec):
            self._require_task1_attestation(receipt, intent, pair.first)
        running = replace_step(receipt, intent)
        self._write(running)
        match operation:
            case "create" | "update":
                return self._submit_write(running, intent, spec, pair)
            case "noop":
                return self._submit_noop(running, intent, spec, pair.first)
            case _ as unreachable_operation:
                assert_never(unreachable_operation)

    def _recover_intent(
        self,
        receipt: CoderSecretReceipt,
        intent: CoderSecretReceiptStep,
        spec: CoderSecretSpec,
        pair: MetadataPair,
    ) -> CoderSecretReceipt:
        match intent.operation:
            case "create":
                if pair.first is None:
                    return self._submit_write(receipt, intent, spec, pair)
                if not metadata_matches(pair.first, spec):
                    self._block(receipt, intent, "metadata drift")
                submitted = replace(
                    intent,
                    secret_id=pair.first.secret_id,
                    status="submitted",
                    response_sha256=metadata_hash(pair.first),
                    updated_at=now(),
                )
                resumed = replace_step(receipt, submitted)
                self._write(resumed)
                return self._verify_submitted(resumed, submitted, spec)
            case "update":
                self._block(receipt, intent, "ambiguous update intent")
            case "noop":
                self._require_current_metadata(receipt, intent, spec, pair.first)
                return self._submit_noop(receipt, intent, spec, pair.first)
            case _ as unreachable_operation:
                assert_never(unreachable_operation)

    def _submit_write(
        self,
        receipt: CoderSecretReceipt,
        intent: CoderSecretReceiptStep,
        spec: CoderSecretSpec,
        pair: MetadataPair,
    ) -> CoderSecretReceipt:
        third = self._metadata(spec.name)
        if metadata_hash(third) != pair.first_sha256:
            self._block(receipt, intent, "metadata drift")
        response_sha256 = self._client.write_secret(intent.operation, spec)
        if not valid_hash(response_sha256):
            self._block(receipt, intent, "write response is invalid")
        submitted = replace(
            intent,
            status="submitted",
            response_sha256=response_sha256,
            updated_at=now(),
        )
        running = replace_step(receipt, submitted)
        self._write(running)
        return self._verify_submitted(running, submitted, spec)

    def _submit_noop(
        self,
        receipt: CoderSecretReceipt,
        intent: CoderSecretReceiptStep,
        spec: CoderSecretSpec,
        metadata: CoderSecretMetadata | None,
    ) -> CoderSecretReceipt:
        if metadata is None:
            self._block(receipt, intent, "metadata drift")
        submitted = replace(
            intent,
            status="submitted",
            response_sha256=metadata_hash(metadata),
            updated_at=now(),
        )
        running = replace_step(receipt, submitted)
        self._write(running)
        return self._verify_submitted(running, submitted, spec)

    def _verify_submitted(
        self, receipt: CoderSecretReceipt, submitted: CoderSecretReceiptStep, spec: CoderSecretSpec
    ) -> CoderSecretReceipt:
        pair = self._metadata_pair(spec.name)
        if not pair.stable:
            self._block(receipt, submitted, "metadata drift")
        self._require_current_metadata(receipt, submitted, spec, pair.first)
        if submitted.operation == "create" and submitted.secret_id is None:
            if pair.first is None:
                self._block(receipt, submitted, "metadata drift")
            submitted = replace(submitted, secret_id=pair.first.secret_id, updated_at=now())
            receipt = replace_step(receipt, submitted)
            self._write(receipt)
        workspace_hash = self._client.verify_workspace_value_hash(spec, self._owner_id)
        if workspace_hash != value_hash(spec.value):
            self._block(receipt, submitted, "workspace verification failed")
        verified = replace(
            submitted,
            status="verified",
            workspace_verification_sha256=workspace_hash,
            updated_at=now(),
        )
        result = replace_step(receipt, verified)
        self._write(result)
        return result

    def _metadata_pair(self, name: str) -> MetadataPair:
        return MetadataPair(first=self._metadata(name), second=self._metadata(name))

    def _metadata(self, name: str) -> CoderSecretMetadata | None:
        matches = tuple(secret for secret in self._client.list_secrets() if secret.name == name)
        if len(matches) > 1:
            raise CoderSecretError("Coder secret metadata is ambiguous", kind="metadata")
        return matches[0] if matches else None

    def _is_owned(
        self,
        previous: CoderSecretReceiptStep | None,
        metadata: CoderSecretMetadata,
        spec: CoderSecretSpec,
    ) -> bool:
        return (
            previous is not None
            and previous.status == "verified"
            and previous.secret_id == metadata.secret_id
            and metadata_matches(metadata, spec)
        )

    def _require_current_metadata(
        self,
        receipt: CoderSecretReceipt,
        step: CoderSecretReceiptStep,
        spec: CoderSecretSpec,
        metadata: CoderSecretMetadata | None,
    ) -> None:
        if (
            metadata is None
            or not metadata_matches(metadata, spec)
            or (step.secret_id is not None and step.secret_id != metadata.secret_id)
        ):
            self._block(receipt, step, "metadata drift")

    def _require_task1_attestation(
        self,
        receipt: CoderSecretReceipt,
        intent: CoderSecretReceiptStep,
        metadata: CoderSecretMetadata,
    ) -> None:
        try:
            attested = proves_task1_created_secret(
                secret_id=metadata.secret_id,
                name=metadata.name,
                env_name=metadata.env_name,
                description=metadata.description,
            )
        except Task1CoderSecretAttestationError:
            self._block(receipt, intent, "Task 1 attestation is invalid")
        if not attested:
            self._block(receipt, intent, "ownership is unproven")

    def _block(
        self, receipt: CoderSecretReceipt, step: CoderSecretReceiptStep, reason: str
    ) -> NoReturn:
        blocked = replace(step, status="blocked", updated_at=now())
        self._write(replace(replace_step(receipt, blocked), status="blocked"))
        raise CoderSecretError(
            f"Coder secret reconciliation blocked: {reason}",
            kind="blocked",
        )

    def _write(self, receipt: CoderSecretReceipt) -> None:
        try:
            self._store.write(receipt)
        except CoderSecretReceiptError as error:
            raise CoderSecretError(
                "Coder secret receipt cannot be written",
                kind="receipt_invalid",
            ) from error
