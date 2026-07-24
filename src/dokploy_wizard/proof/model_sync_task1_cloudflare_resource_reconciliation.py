"""Receipt-bound intent construction for Task 1 proof-created Cloudflare resources."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from dokploy_wizard.proof.model_sync_task1_cloudflare_create_recovery import (
    Task1CreateRecoveryRequest,
    recover_or_create_task1_resource,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    absence_fingerprint,
    operation_key,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
    Task1CloudflareJournalIntent,
)

Resource = TypeVar("Resource")


@dataclass(frozen=True, slots=True)
class Task1ProofResourceRequest(Generic[Resource]):
    """Typed complete collision scope and provider adapters for one proof-only creation."""

    kind: JournalOperationKind
    account_id: str
    zone_id: str | None
    parent_id: str | None
    scope: Mapping[str, str]
    expected_name_sha256: str | None
    expected_domain_sha256: str | None
    desired_fingerprint: str
    resource_label: str
    matches: Callable[[], tuple[Resource, ...]]
    create: Callable[[], Resource]
    resource_id: Callable[[Resource], str]
    fingerprint: Callable[[Resource], str]
    observe: Callable[[str], Resource | None]
    error_type: type[Exception]


def resolve_task1_proof_resource(
    *, journal: Task1CloudflareJournal, request: Task1ProofResourceRequest[Resource]
) -> Resource:
    """Persist absence-bound intent before creating or exactly adopting a crash-recovered object."""
    intent = Task1CloudflareJournalIntent(
        logical_key=operation_key(request.kind, request.scope),
        kind=request.kind,
        account_id=request.account_id,
        zone_id=request.zone_id,
        parent_id=request.parent_id,
        expected_name_sha256=request.expected_name_sha256,
        expected_domain_sha256=request.expected_domain_sha256,
        pre_absence_sha256=absence_fingerprint(request.kind, request.scope),
        desired_spec_sha256=request.desired_fingerprint,
    )
    operation = journal.existing_operation(intent)
    if operation is None:
        if request.matches():
            raise request.error_type(
                f"Task 1 proof context requires an absent Cloudflare {request.resource_label}"
            )
        operation = journal.record_intent(intent)
    return recover_or_create_task1_resource(
        journal=journal,
        request=Task1CreateRecoveryRequest(
            operation=operation,
            desired_fingerprint=request.desired_fingerprint,
            matches=request.matches,
            create=request.create,
            resource_id=request.resource_id,
            fingerprint=request.fingerprint,
            observe=request.observe,
            error_type=request.error_type,
            resource_label=request.resource_label,
        ),
    )
