from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn, Protocol

from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    CoderWorkspaceRunner,
    WorkspaceIdentityError,
    WorkspaceRecord,
    WorkspaceVerificationIntent,
)
from dokploy_wizard.dokploy.coder_secret_workspace_inventory import workspace_records
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPhase,
    WorkspaceVerificationReceipt,
    WorkspaceVerificationReceiptStore,
)


def require_matching_receipt(
    receipt: WorkspaceVerificationReceipt,
    intent: WorkspaceVerificationIntent,
) -> None:
    if (
        receipt.owner_id != intent.owner_id
        or receipt.env_name != intent.env_name
        or receipt.expected_value_sha256 != intent.expected_value_sha256
    ):
        raise CoderSecretClientError(
            "Coder workspace verification receipt ownership does not match"
        )


def block_identity(
    store: WorkspaceVerificationReceiptStore, error: WorkspaceIdentityError
) -> NoReturn:
    receipt = store.load()
    if receipt is not None:
        store.transition(
            receipt,
            WorkspaceVerificationPhase.BLOCKED,
            observed_value_sha256=receipt.observed_value_sha256,
            failure_reason="identity_drift",
        )
    raise error


class WorkspaceReceiptCleaner(Protocol):
    def _cleanup(
        self,
        store: WorkspaceVerificationReceiptStore,
        receipt: WorkspaceVerificationReceipt,
        terminal_phase: WorkspaceVerificationPhase,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkspaceFailure:
    phase: WorkspaceVerificationPhase
    reason: str
    primary: CoderSecretClientError


def create_or_bind_planned(
    runner: CoderWorkspaceRunner,
    store: WorkspaceVerificationReceiptStore,
    receipt: WorkspaceVerificationReceipt,
) -> WorkspaceVerificationReceipt:
    candidates = _planned_candidates(runner, receipt)
    match candidates:
        case ():
            return create_planned(runner, store, receipt)
        case (workspace,):
            return _bind_candidate(store, receipt, workspace)
        case _:
            raise WorkspaceIdentityError("Coder verification workspace identity drifted")


def create_planned(
    runner: CoderWorkspaceRunner,
    store: WorkspaceVerificationReceiptStore,
    receipt: WorkspaceVerificationReceipt,
) -> WorkspaceVerificationReceipt:
    attempted = store.begin_create(receipt)
    runner(("create", "--yes", "--template", attempted.template_name, attempted.workspace_name))
    return _bind_planned(runner, store, attempted)


def _bind_planned(
    runner: CoderWorkspaceRunner,
    store: WorkspaceVerificationReceiptStore,
    receipt: WorkspaceVerificationReceipt,
) -> WorkspaceVerificationReceipt:
    candidates = _planned_candidates(runner, receipt)
    match candidates:
        case (workspace,):
            return _bind_candidate(store, receipt, workspace)
        case _:
            raise WorkspaceIdentityError("Coder verification workspace identity drifted")


def _planned_candidates(
    runner: CoderWorkspaceRunner, receipt: WorkspaceVerificationReceipt
) -> tuple[WorkspaceRecord, ...]:
    return tuple(
        workspace
        for workspace in workspace_records(runner(("list", "--output", "json")))
        if workspace.workspace_name == receipt.workspace_name
    )


def _bind_candidate(
    store: WorkspaceVerificationReceiptStore,
    receipt: WorkspaceVerificationReceipt,
    candidate: WorkspaceRecord,
) -> WorkspaceVerificationReceipt:
    if (
        candidate.template_id != receipt.template_id
        or candidate.template_name != receipt.template_name
    ):
        raise WorkspaceIdentityError("Coder verification workspace identity drifted")
    return store.bind_workspace(candidate.workspace_id, candidate.owner_id, candidate.owner_name)


def finish_resumed(
    cleaner: WorkspaceReceiptCleaner,
    store: WorkspaceVerificationReceiptStore,
    receipt: WorkspaceVerificationReceipt,
) -> str:
    observed = receipt.observed_value_sha256
    if observed is None:
        raise CoderSecretClientError("Coder workspace verification receipt is incomplete")
    cleaner._cleanup(store, receipt, WorkspaceVerificationPhase.DELETED)
    return observed


def fail_after_cleanup(
    cleaner: WorkspaceReceiptCleaner,
    store: WorkspaceVerificationReceiptStore,
    failure: WorkspaceFailure,
) -> NoReturn:
    receipt = store.load()
    if receipt is None:
        raise failure.primary
    failed = store.transition(
        receipt,
        failure.phase,
        observed_value_sha256=receipt.observed_value_sha256,
        failure_reason=failure.reason,
    )
    if receipt.workspace_id is None or receipt.phase is WorkspaceVerificationPhase.DELETING:
        raise failure.primary
    try:
        cleaner._cleanup(store, failed, failure.phase)
    except CoderSecretClientError:
        store.transition(
            failed,
            failure.phase,
            observed_value_sha256=failed.observed_value_sha256,
            failure_reason=f"{failure.reason}_cleanup_failure",
        )
        raise CoderSecretClientError(
            "Coder verification failed and cleanup failed"
        ) from failure.primary
    raise failure.primary
