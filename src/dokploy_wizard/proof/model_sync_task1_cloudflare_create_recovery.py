"""Generic crash-resumable create-only recovery for receipt-owned resources."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    valid_provider_identifier,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationState,
    Task1CloudflareJournal,
    Task1CloudflareJournalOperation,
)

Resource = TypeVar("Resource")


@dataclass(frozen=True, slots=True)
class Task1CreateRecoveryRequest(Generic[Resource]):
    """Typed provider adapters and the exact fingerprinted create contract."""

    operation: Task1CloudflareJournalOperation
    desired_fingerprint: str
    matches: Callable[[], tuple[Resource, ...]]
    create: Callable[[], Resource]
    resource_id: Callable[[Resource], str]
    fingerprint: Callable[[Resource], str]
    observe: Callable[[str], Resource | None]
    error_type: type[Exception]
    resource_label: str


def recover_or_create_task1_resource(
    *,
    journal: Task1CloudflareJournal,
    request: Task1CreateRecoveryRequest[Resource],
) -> Resource:
    """Adopt only one exact desired object or create and verify it after durable intent."""
    matches = request.matches()
    if len(matches) > 1 or (
        matches and request.fingerprint(matches[0]) != request.desired_fingerprint
    ):
        raise request.error_type(
            f"Task 1 Cloudflare {request.resource_label} recovery found collision or drift"
        )
    if matches:
        recovered = matches[0]
        _checkpoint_recovered(journal, request, recovered)
        return recovered
    if request.operation.state is JournalOperationState.CREATED:
        raise request.error_type(
            f"Task 1 Cloudflare {request.resource_label} disappeared after creation checkpoint"
        )
    if request.operation.state is not JournalOperationState.INTENT:
        raise request.error_type(f"Task 1 Cloudflare {request.resource_label} intent state drifted")
    created = request.create()
    journal.record_provider_response(request.operation)
    identifier = request.resource_id(created)
    if not valid_provider_identifier(identifier):
        raise request.error_type(
            f"Task 1 Cloudflare {request.resource_label} returned an invalid resource ID"
        )
    observed = request.observe(identifier)
    if observed is None or request.fingerprint(observed) != request.desired_fingerprint:
        raise request.error_type(f"Task 1 Cloudflare {request.resource_label} response drifted")
    journal.checkpoint_created(
        request.operation,
        response_id=identifier,
        post_fingerprint_sha256=request.desired_fingerprint,
    )
    return observed


def _checkpoint_recovered(
    journal: Task1CloudflareJournal,
    request: Task1CreateRecoveryRequest[Resource],
    recovered: Resource,
) -> None:
    """Require a durable response to match the unique observed resource exactly."""
    identifier = request.resource_id(recovered)
    if request.operation.state is JournalOperationState.CREATED:
        if (
            request.operation.response_id != identifier
            or request.operation.post_fingerprint_sha256 != request.desired_fingerprint
        ):
            raise request.error_type(f"Task 1 Cloudflare {request.resource_label} recovery drifted")
        return
    if request.operation.state is not JournalOperationState.INTENT:
        raise request.error_type(f"Task 1 Cloudflare {request.resource_label} intent state drifted")
    journal.checkpoint_created(
        request.operation,
        response_id=identifier,
        post_fingerprint_sha256=request.desired_fingerprint,
    )
