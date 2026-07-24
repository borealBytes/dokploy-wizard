"""Bounded provider-inventory resolution for incomplete Task 1 journal intents."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, TypeVar

from dokploy_wizard.networking.cloudflare import CloudflareAccessApplication, CloudflareAccessPolicy
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    application_fingerprint,
    configuration_fingerprint,
    dns_fingerprint,
    policy_fingerprint,
    snapshot_value_hash,
    tunnel_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_runtime import (
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalOperationKind,
    JournalOperationState,
    Task1CloudflareJournalOperation,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotBackend,
    CloudflareSnapshotPage,
    CloudflareSnapshotScope,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
    CloudflareSnapshotCollectionError,
)

_PAGE_SIZE = 100
_MAX_PAGES = 100
Resource = TypeVar("Resource")


class Task1ConfigurationRecoveryBackend(Protocol):
    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[Mapping[str, object], ...]: ...


def reconcile_incomplete_intents(
    *,
    journal: Task1CloudflareJournal,
    backend: CloudflareSnapshotBackend,
    scope: CloudflareSnapshotScope,
) -> None:
    """Checkpoint exact provider matches or durably abort unprovable intended writes."""
    intents = tuple(
        operation
        for operation in journal.document().operations
        if operation.state is JournalOperationState.INTENT
    )
    if not intents:
        return
    tunnels = _all_pages(
        lambda page: backend.list_tunnels_page(scope.account_id, page, _PAGE_SIZE),
        lambda tunnel: tunnel.tunnel_id,
    )
    records = _all_pages(
        lambda page: backend.list_dns_records_page(scope.zone_id, page, _PAGE_SIZE),
        lambda record: record.record_id,
    )
    applications = _all_pages(
        lambda page: backend.list_access_applications_page(scope.account_id, page, _PAGE_SIZE),
        _application_id,
    )
    policies = tuple(
        policy
        for application in applications
        for policy in _policy_pages(
            backend,
            account_id=scope.account_id,
            app_id=application.app_id,
        )
    )
    for operation in intents:
        match operation.intent.kind:
            case JournalOperationKind.TUNNEL:
                _resolve_resources(
                    journal,
                    operation,
                    tuple(
                        (tunnel.tunnel_id, tunnel_fingerprint(tunnel))
                        for tunnel in tunnels
                        if operation.intent.expected_name_sha256 == snapshot_value_hash(tunnel.name)
                    ),
                )
            case JournalOperationKind.DNS_RECORD:
                _resolve_resources(
                    journal,
                    operation,
                    tuple(
                        (record.record_id, dns_fingerprint(record))
                        for record in records
                        if operation.intent.expected_name_sha256 == snapshot_value_hash(record.name)
                    ),
                )
            case JournalOperationKind.ACCESS_APPLICATION:
                _resolve_resources(
                    journal,
                    operation,
                    tuple(
                        (application.app_id, application_fingerprint(application))
                        for application in applications
                        if operation.intent.expected_domain_sha256
                        == snapshot_value_hash(application.domain)
                    ),
                )
            case JournalOperationKind.ACCESS_POLICY:
                _resolve_resources(
                    journal,
                    operation,
                    tuple(
                        (policy.policy_id, policy_fingerprint(policy))
                        for policy in policies
                        if policy.app_id == operation.intent.parent_id
                        and operation.intent.expected_name_sha256
                        == snapshot_value_hash(policy.name)
                    ),
                )
            case JournalOperationKind.TUNNEL_CONFIGURATION:
                _resolve_configuration(
                    journal,
                    backend,
                    operation,
                    frozenset(tunnel.tunnel_id for tunnel in tunnels),
                )
            case unreachable:
                raise AssertionError(f"unexpected journal kind: {unreachable}")


def _resolve_resources(
    journal: Task1CloudflareJournal,
    operation: Task1CloudflareJournalOperation,
    candidates: tuple[tuple[str, str], ...],
) -> None:
    if not candidates:
        journal.checkpoint_aborted(operation)
        return
    if len(candidates) != 1:
        raise CloudflareSnapshotCollectionError("Cloudflare intent recovery is ambiguous")
    resource_id, fingerprint = candidates[0]
    if fingerprint != operation.intent.desired_spec_sha256:
        raise CloudflareSnapshotCollectionError("Cloudflare intent recovery drifted")
    journal.checkpoint_created(
        operation,
        response_id=resource_id,
        post_fingerprint_sha256=fingerprint,
    )


def _resolve_configuration(
    journal: Task1CloudflareJournal,
    backend: Task1ConfigurationRecoveryBackend,
    operation: Task1CloudflareJournalOperation,
    tunnel_ids: frozenset[str],
) -> None:
    parent_id = operation.intent.parent_id
    if parent_id is None or parent_id not in tunnel_ids:
        raise CloudflareSnapshotCollectionError("Cloudflare configuration intent parent is missing")
    configuration = backend.get_tunnel_configuration(operation.intent.account_id, parent_id)
    fingerprint = configuration_fingerprint(tuple(dict(item) for item in configuration))
    if fingerprint == operation.intent.pre_absence_sha256:
        journal.checkpoint_aborted(operation)
        return
    if fingerprint != operation.intent.desired_spec_sha256:
        raise CloudflareSnapshotCollectionError("Cloudflare configuration intent recovery drifted")
    journal.checkpoint_created(operation, response_id=None, post_fingerprint_sha256=fingerprint)


def _all_pages(
    fetch: Callable[[int], CloudflareSnapshotPage[Resource]],
    identifier: Callable[[Resource], str],
) -> tuple[Resource, ...]:
    first = fetch(1)
    if first.page != 1 or first.per_page != _PAGE_SIZE or first.total_count < 0:
        raise CloudflareSnapshotCollectionError("Cloudflare inventory pagination is invalid")
    pages = max(1, (first.total_count + _PAGE_SIZE - 1) // _PAGE_SIZE)
    if first.total_pages != pages or pages > _MAX_PAGES:
        raise CloudflareSnapshotCollectionError("Cloudflare inventory pagination is incomplete")
    collected = list(first.items)
    for page_number in range(2, pages + 1):
        page = fetch(page_number)
        if (
            page.page != page_number
            or page.per_page != _PAGE_SIZE
            or page.total_count != first.total_count
            or page.total_pages != pages
        ):
            raise CloudflareSnapshotCollectionError("Cloudflare inventory pagination changed")
        collected.extend(page.items)
    if len(collected) != first.total_count or len({identifier(item) for item in collected}) != len(
        collected
    ):
        raise CloudflareSnapshotCollectionError("Cloudflare inventory is truncated or duplicated")
    return tuple(collected)


def _application_id(application: CloudflareAccessApplication) -> str:
    return application.app_id


def _policy_id(policy: CloudflareAccessPolicy) -> str:
    return policy.policy_id


def _policy_pages(
    backend: CloudflareSnapshotBackend, *, account_id: str, app_id: str
) -> tuple[CloudflareAccessPolicy, ...]:
    def fetch(page: int) -> CloudflareSnapshotPage[CloudflareAccessPolicy]:
        return backend.list_access_policies_page(account_id, app_id, page, _PAGE_SIZE)

    return _all_pages(fetch, _policy_id)
