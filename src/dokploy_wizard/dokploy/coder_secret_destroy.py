"""Receipt-authorized destroy-mode cleanup for Coder workspace secrets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

from dokploy_wizard.dokploy.coder_secret_destroy_authorization import (
    secret_id,
    source_receipt,
    source_receipt_sha256,
)
from dokploy_wizard.dokploy.coder_secret_destroy_contract import (
    CoderSecretDestroyClient,
    CoderSecretDestroyError,
)
from dokploy_wizard.dokploy.coder_secret_destroy_receipts import (
    CoderSecretDestroyReceipt,
    CoderSecretDestroyReceiptError,
    CoderSecretDestroyReceiptStore,
    CoderSecretDestroyStep,
)
from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptStep,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretMetadata
from dokploy_wizard.dokploy.coder_secret_reconciliation_types import metadata_hash, now
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPhase,
    WorkspaceVerificationReceiptStore,
)


@dataclass(frozen=True, slots=True)
class CoderSecretDestroyer:
    state_dir: Path
    client: CoderSecretDestroyClient
    owner_id: str

    def destroy(self) -> None:
        source = source_receipt(self.state_dir, self.owner_id)
        store = CoderSecretDestroyReceiptStore(self.state_dir)
        receipt = self._destroy_receipt(store, source)
        for source_step in source.steps:
            receipt = self._destroy_one(store, receipt, source_step)
        completed = replace(receipt, status="completed")
        store.write(completed)
        self._verify_all_absent(source)
        self._verify_workspace_absence()

    def _destroy_receipt(
        self,
        store: CoderSecretDestroyReceiptStore,
        source: CoderSecretReceipt,
    ) -> CoderSecretDestroyReceipt:
        source_sha256 = source_receipt_sha256(source)
        try:
            existing = store.load()
        except CoderSecretDestroyReceiptError as error:
            raise CoderSecretDestroyError(
                "Coder secret destroy receipt is invalid"
            ) from error
        if existing is None:
            return CoderSecretDestroyReceipt(
                owner_id=self.owner_id,
                source_receipt_sha256=source_sha256,
                status="running",
                steps=(),
            )
        if (
            existing.owner_id != self.owner_id
            or existing.source_receipt_sha256 != source_sha256
            or existing.status == "blocked"
        ):
            raise CoderSecretDestroyError("Coder secret destroy receipt does not authorize resume")
        self._validate_destroy_steps(existing, source)
        return existing

    def _destroy_one(
        self,
        store: CoderSecretDestroyReceiptStore,
        receipt: CoderSecretDestroyReceipt,
        source: CoderSecretReceiptStep,
    ) -> CoderSecretDestroyReceipt:
        previous = next(
            (step for step in receipt.steps if step.secret_name == source.secret_name), None
        )
        try:
            observed = self._observe(source)
        except CoderSecretDestroyError:
            self._block(store, receipt, source, metadata_hash(None))
        if previous is not None and previous.status == "deleted":
            if observed is not None:
                self._block(store, receipt, source, metadata_hash(observed))
            return receipt
        intent = previous or CoderSecretDestroyStep(
            secret_id=secret_id(source),
            secret_name=source.secret_name,
            env_name=source.env_name,
            description=source.description,
            status="intent",
            pre_metadata_sha256=metadata_hash(observed),
            post_metadata_sha256=None,
            updated_at=now(),
        )
        if observed is None:
            return self._record_deleted(store, receipt, intent)
        running = self._replace_step(receipt, intent)
        store.write(running)
        try:
            self.client.delete_secret(observed)
        except RuntimeError as error:
            raise CoderSecretDestroyError("Coder secret delete failed") from error
        try:
            remains = self._observe(source)
        except CoderSecretDestroyError:
            self._block(store, running, source, metadata_hash(None))
        if remains is not None:
            self._block(store, running, source, metadata_hash(observed))
        return self._record_deleted(store, running, intent)

    def _observe(self, source: CoderSecretReceiptStep) -> CoderSecretMetadata | None:
        expected_id = secret_id(source)
        secrets = self.client.list_secrets()
        by_id = tuple(secret for secret in secrets if secret.secret_id == expected_id)
        by_name = tuple(secret for secret in secrets if secret.name == source.secret_name)
        if not by_id and not by_name:
            return None
        if len(by_id) != 1 or len(by_name) != 1 or by_id[0] != by_name[0]:
            raise CoderSecretDestroyError("Coder secret identity drift blocks destroy")
        candidate = by_id[0]
        if (
            candidate.env_name != source.env_name
            or candidate.description != source.description
            or candidate.name != source.secret_name
        ):
            raise CoderSecretDestroyError("Coder secret metadata drift blocks destroy")
        return candidate

    def _record_deleted(
        self,
        store: CoderSecretDestroyReceiptStore,
        receipt: CoderSecretDestroyReceipt,
        intent: CoderSecretDestroyStep,
    ) -> CoderSecretDestroyReceipt:
        deleted = replace(
            intent,
            status="deleted",
            post_metadata_sha256=metadata_hash(None),
            updated_at=now(),
        )
        updated = self._replace_step(receipt, deleted)
        store.write(updated)
        return updated

    def _verify_all_absent(self, source: CoderSecretReceipt) -> None:
        for step in source.steps:
            if self._observe(step) is not None:
                raise CoderSecretDestroyError("Coder secret remains after destroy")

    def _verify_workspace_absence(self) -> None:
        store = WorkspaceVerificationReceiptStore(self.state_dir)
        receipt = store.load()
        if receipt is None:
            return
        if receipt.owner_id != self.owner_id:
            raise CoderSecretDestroyError("Coder workspace verification receipt owner is invalid")
        if receipt.phase is not WorkspaceVerificationPhase.DELETED:
            raise CoderSecretDestroyError("Coder workspace verification receipt is not terminal")
        try:
            self.client.require_workspace_receipt_absence(receipt)
        except RuntimeError as error:
            raise CoderSecretDestroyError(
                "Coder workspace verification absence cannot be proved"
            ) from error

    def _validate_destroy_steps(
        self, receipt: CoderSecretDestroyReceipt, source: CoderSecretReceipt
    ) -> None:
        expected = {step.secret_name: step for step in source.steps}
        if any(step.secret_name not in expected for step in receipt.steps):
            raise CoderSecretDestroyError("Coder secret destroy receipt has an unknown secret")
        for step in receipt.steps:
            source_step = expected[step.secret_name]
            if (
                step.secret_id != secret_id(source_step)
                or step.env_name != source_step.env_name
                or step.description != source_step.description
            ):
                raise CoderSecretDestroyError("Coder secret destroy receipt identity drifted")

    def _replace_step(
        self, receipt: CoderSecretDestroyReceipt, step: CoderSecretDestroyStep
    ) -> CoderSecretDestroyReceipt:
        remaining = tuple(
            item for item in receipt.steps if item.secret_name != step.secret_name
        )
        steps = tuple(
            sorted(
                (*remaining, step),
                key=lambda item: item.secret_name,
            )
        )
        return replace(receipt, status="running", steps=steps)

    def _block(
        self,
        store: CoderSecretDestroyReceiptStore,
        receipt: CoderSecretDestroyReceipt,
        source: CoderSecretReceiptStep,
        observed_sha256: str,
    ) -> NoReturn:
        blocked = CoderSecretDestroyStep(
            secret_id=secret_id(source),
            secret_name=source.secret_name,
            env_name=source.env_name,
            description=source.description,
            status="blocked",
            pre_metadata_sha256=observed_sha256,
            post_metadata_sha256=None,
            updated_at=now(),
        )
        store.write(replace(self._replace_step(receipt, blocked), status="blocked"))
        raise CoderSecretDestroyError("Coder secret identity drift blocks destroy")


__all__ = ["CoderSecretDestroyError", "CoderSecretDestroyer"]
