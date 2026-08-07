from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_fs import (
    read_receipt_bytes,
    write_receipt_bytes,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema import (
    parse_receipt_bytes,
    receipt_bytes,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_types import (
    MAX_CREATE_ATTEMPTS,
    WorkspaceVerificationPhase,
    WorkspaceVerificationPlan,
    WorkspaceVerificationReceipt,
)

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
        CoderWorkspaceRunner,
        WorkspaceVerificationIntent,
    )

WORKSPACE_VERIFICATION_RECEIPT_FILENAME = "coder-workspace-verification-receipt.json"


class WorkspaceVerificationReceiptStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    def load(self) -> WorkspaceVerificationReceipt | None:
        payload = self.load_bytes()
        return None if payload is None else parse_receipt_bytes(payload)

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def load_bytes(self) -> bytes | None:
        return read_receipt_bytes(
            self._state_dir, WORKSPACE_VERIFICATION_RECEIPT_FILENAME
        )

    def write_planned(self, plan: WorkspaceVerificationPlan) -> WorkspaceVerificationReceipt:
        now = _now()
        receipt = WorkspaceVerificationReceipt(
            owner_id=plan.owner_id, workspace_id=None, workspace_name=plan.workspace_name,
            workspace_owner_id=None, workspace_owner_name=None, template_id=plan.template_id,
            template_name=plan.template_name, env_name=plan.env_name,
            expected_value_sha256=plan.expected_value_sha256, observed_value_sha256=None,
            create_attempts=0, phase=WorkspaceVerificationPhase.PLANNED, failure_reason=None,
            created_at=now, updated_at=now,
        )
        self.write(receipt)
        return receipt

    def begin_create(self, receipt: WorkspaceVerificationReceipt) -> WorkspaceVerificationReceipt:
        match receipt.phase:
            case WorkspaceVerificationPhase.PLANNED:
                if receipt.create_attempts == MAX_CREATE_ATTEMPTS:
                    exhausted = replace(
                        receipt, phase=WorkspaceVerificationPhase.FAILED,
                        failure_reason="create_retry_exhausted", updated_at=_now(),
                    )
                    self.write(exhausted)
                    raise CoderSecretClientError(
                        "Coder workspace verification create retries exhausted",
                        kind="client_workspace_create",
                    )
                attempted = replace(
                    receipt,
                    create_attempts=receipt.create_attempts + 1,
                    updated_at=_now(),
                )
                self.write(attempted)
                return attempted
            case _:
                raise CoderSecretClientError(
                    "Coder workspace verification receipt cannot create",
                    kind="client_workspace_receipt_state",
                )

    def bind_workspace(
        self, workspace_id: str, workspace_owner_id: str, workspace_owner_name: str
    ) -> WorkspaceVerificationReceipt:
        receipt = self._require_current()
        match receipt.phase:
            case WorkspaceVerificationPhase.PLANNED:
                bound = replace(
                    receipt,
                    workspace_id=workspace_id,
                    workspace_owner_id=workspace_owner_id,
                    workspace_owner_name=workspace_owner_name,
                    phase=WorkspaceVerificationPhase.CREATED,
                    updated_at=_now(),
                )
                self.write(bound)
                return bound
            case _:
                raise CoderSecretClientError(
                    "Coder workspace verification receipt cannot bind",
                    kind="client_workspace_receipt_state",
                )

    def transition(
        self, receipt: WorkspaceVerificationReceipt, phase: WorkspaceVerificationPhase,
        *, observed_value_sha256: str | None = None, failure_reason: str | None = None,
    ) -> WorkspaceVerificationReceipt:
        updated = replace(
            receipt, phase=phase, observed_value_sha256=observed_value_sha256,
            failure_reason=failure_reason, updated_at=_now(),
        )
        self.write(updated)
        return updated

    def write(self, receipt: WorkspaceVerificationReceipt) -> None:
        write_receipt_bytes(
            self._state_dir,
            WORKSPACE_VERIFICATION_RECEIPT_FILENAME,
            receipt_bytes(receipt),
        )

    def replace_exact(
        self, expected: bytes, receipt: WorkspaceVerificationReceipt
    ) -> None:
        if self.load_bytes() != expected:
            raise CoderSecretClientError(
                "Coder workspace verification receipt changed before supersession",
                kind="client_workspace_receipt_state",
            )
        self.write(receipt)

    def supersede_authorized(
        self,
        runner: CoderWorkspaceRunner,
        receipt: WorkspaceVerificationReceipt,
        intent: WorkspaceVerificationIntent,
    ) -> WorkspaceVerificationReceipt:
        from dokploy_wizard.dokploy.coder_secret_workspace_supersession import (
            supersede_exhausted_receipt,
        )

        if receipt.phase is not WorkspaceVerificationPhase.FAILED:
            return receipt
        authorization = os.environ.get(
            "DOKPLOY_WIZARD_CODER_VERIFIER_SUPERSESSION_AUTHORIZATION"
        )
        if authorization is None:
            return receipt
        return supersede_exhausted_receipt(runner, self, intent, Path(authorization))

    def remove(self, receipt: WorkspaceVerificationReceipt) -> None:
        expected = receipt_bytes(receipt)
        current = self.load_bytes()
        if current != expected:
            raise CoderSecretClientError(
                "Coder workspace verification receipt changed before removal",
                kind="client_workspace_receipt_state",
            )
        try:
            (self._state_dir / WORKSPACE_VERIFICATION_RECEIPT_FILENAME).unlink()
            descriptor = os.open(
                self._state_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise CoderSecretClientError(
                "Coder workspace verification receipt cannot be removed",
                kind="client_workspace_receipt_write",
            ) from error

    def _require_current(self) -> WorkspaceVerificationReceipt:
        receipt = self.load()
        if receipt is None:
            raise CoderSecretClientError(
                "Coder workspace verification receipt is missing",
                kind="client_workspace_receipt_state",
            )
        return receipt


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "MAX_CREATE_ATTEMPTS", "WorkspaceVerificationPhase", "WorkspaceVerificationPlan",
    "WorkspaceVerificationReceipt", "WorkspaceVerificationReceiptStore",
]
