"""Receipt-owned reverse dependency cleanup for the Task 1 Cloudflare journal."""

from __future__ import annotations

from typing import Protocol

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.proof import CaptureSchemaError
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    configuration_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_cleanup_helpers import (
    cleanup_order,
    resource_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_runtime import (
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalCleanupState,
    JournalOperationKind,
    JournalOperationState,
    Task1CloudflareJournalOperation,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError, sha256


class Task1CloudflareCleanupBackend(Protocol):
    """Read/delete capabilities used only after a complete journal validates."""

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None: ...

    def delete_tunnel(self, account_id: str, tunnel_id: str) -> None: ...

    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[dict[str, object], ...]: ...

    def get_dns_record(self, zone_id: str, record_id: str) -> CloudflareDnsRecord | None: ...

    def delete_dns_record(self, zone_id: str, record_id: str) -> None: ...

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None: ...

    def delete_access_application(self, account_id: str, app_id: str) -> None: ...

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None: ...

    def delete_access_policy(self, account_id: str, app_id: str, policy_id: str) -> None: ...


def cleanup_journal(
    journal: Task1CloudflareJournal, backend: Task1CloudflareCleanupBackend
) -> dict[str, str]:
    """Resume verified deletion without ever adopting a foreign object at a recorded ID."""
    document = journal.document()
    if not document.operations:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal has no operations")
    if any(operation.state is JournalOperationState.INTENT for operation in document.operations):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup found an incomplete intent")
    created = tuple(
        operation
        for operation in document.operations
        if operation.state is JournalOperationState.CREATED
        and operation.intent.kind is not JournalOperationKind.TUNNEL_CONFIGURATION
    )
    for operation in sorted(created, key=cleanup_order):
        if operation.intent.kind is JournalOperationKind.TUNNEL:
            _cleanup_tunnel(journal, backend, operation, document.operations)
            continue
        _cleanup_resource(journal, backend, operation)
    latest = journal.document()
    _verify_tunnel_configuration_cleanup(journal, backend, latest.operations)
    latest = journal.document()
    receipt = {
        "context_sha256": journal.context_sha256,
        "operations_sha256": sha256(latest.to_bytes()),
        "status": latest.status,
    }
    try:
        artifacts.write_or_verify_exact_bytes(
            journal.path.with_suffix(".receipt.json"), _canonical_receipt(receipt)
        )
    except CaptureSchemaError as error:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup receipt is unsafe") from error
    return receipt


def _cleanup_resource(
    journal: Task1CloudflareJournal,
    backend: Task1CloudflareCleanupBackend,
    operation: Task1CloudflareJournalOperation,
) -> None:
    current = _read_resource(backend, operation)
    if current is None:
        journal.checkpoint_cleaned(operation, JournalCleanupState.VERIFIED_ABSENT)
        return
    if resource_fingerprint(current) != operation.post_fingerprint_sha256:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup refused a drifted foreign object")
    _delete_resource(backend, operation)
    if _read_resource(backend, operation) is not None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup did not converge")
    journal.checkpoint_cleaned(operation, JournalCleanupState.VERIFIED_ABSENT)


def _cleanup_tunnel(
    journal: Task1CloudflareJournal,
    backend: Task1CloudflareCleanupBackend,
    tunnel: Task1CloudflareJournalOperation,
    operations: tuple[Task1CloudflareJournalOperation, ...],
) -> None:
    current = _read_resource(backend, tunnel)
    if current is None:
        journal.checkpoint_cleaned(tunnel, JournalCleanupState.VERIFIED_ABSENT)
        return
    if resource_fingerprint(current) != tunnel.post_fingerprint_sha256:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup refused a drifted foreign object")
    _verify_tunnel_configuration(backend, tunnel, operations)
    _delete_resource(backend, tunnel)
    journal.checkpoint_cleaned(tunnel, JournalCleanupState.VERIFIED_ABSENT)


def _verify_tunnel_configuration(
    backend: Task1CloudflareCleanupBackend,
    tunnel: Task1CloudflareJournalOperation,
    operations: tuple[Task1CloudflareJournalOperation, ...],
) -> None:
    for operation in operations:
        if (
            operation.intent.kind is JournalOperationKind.TUNNEL_CONFIGURATION
            and operation.state is JournalOperationState.CREATED
            and operation.intent.parent_id == tunnel.response_id
        ):
            ingress = backend.get_tunnel_configuration(
                tunnel.intent.account_id, tunnel.response_id or ""
            )
            if configuration_fingerprint(ingress) != operation.post_fingerprint_sha256:
                raise Task1ProofContextError("Task 1 Cloudflare tunnel configuration drifted")


def _verify_tunnel_configuration_cleanup(
    journal: Task1CloudflareJournal,
    backend: Task1CloudflareCleanupBackend,
    operations: tuple[Task1CloudflareJournalOperation, ...],
) -> None:
    for operation in operations:
        if operation.intent.kind is not JournalOperationKind.TUNNEL_CONFIGURATION:
            continue
        if operation.state in {JournalOperationState.ABORTED, JournalOperationState.CLEANED}:
            continue
        if operation.intent.parent_id is None:
            raise Task1ProofContextError("Task 1 Cloudflare tunnel configuration parent is missing")
        parent = next(
            (
                candidate
                for candidate in operations
                if candidate.intent.kind is JournalOperationKind.TUNNEL
                and candidate.response_id == operation.intent.parent_id
            ),
            None,
        )
        if parent is None or parent.state is not JournalOperationState.CLEANED:
            raise Task1ProofContextError(
                "Task 1 Cloudflare tunnel configuration cleanup is blocked"
            )
        if backend.get_tunnel(operation.intent.account_id, operation.intent.parent_id) is not None:
            raise Task1ProofContextError(
                "Task 1 Cloudflare tunnel configuration cleanup did not converge"
            )
        journal.checkpoint_cleaned(operation, JournalCleanupState.DELETED_WITH_TUNNEL)


def _read_resource(
    backend: Task1CloudflareCleanupBackend, operation: Task1CloudflareJournalOperation
) -> (
    CloudflareTunnel
    | CloudflareDnsRecord
    | CloudflareAccessApplication
    | CloudflareAccessPolicy
    | None
):
    response_id = operation.response_id
    if response_id is None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup resource ID is absent")
    match operation.intent.kind:
        case JournalOperationKind.TUNNEL:
            return backend.get_tunnel(operation.intent.account_id, response_id)
        case JournalOperationKind.DNS_RECORD:
            if operation.intent.zone_id is None:
                raise Task1ProofContextError("Task 1 Cloudflare DNS cleanup scope is invalid")
            return backend.get_dns_record(operation.intent.zone_id, response_id)
        case JournalOperationKind.ACCESS_APPLICATION:
            return backend.get_access_application(operation.intent.account_id, response_id)
        case JournalOperationKind.ACCESS_POLICY:
            parent_id = operation.intent.parent_id
            if parent_id is None:
                raise Task1ProofContextError("Task 1 Cloudflare policy cleanup scope is invalid")
            return backend.get_access_policy(operation.intent.account_id, parent_id, response_id)
        case JournalOperationKind.TUNNEL_CONFIGURATION:
            raise Task1ProofContextError(
                "Task 1 Cloudflare configuration has no standalone resource"
            )
        case unreachable:
            raise AssertionError(f"unexpected journal kind: {unreachable}")


def _delete_resource(
    backend: Task1CloudflareCleanupBackend, operation: Task1CloudflareJournalOperation
) -> None:
    response_id = operation.response_id
    if response_id is None:
        raise Task1ProofContextError("Task 1 Cloudflare cleanup resource ID is absent")
    match operation.intent.kind:
        case JournalOperationKind.TUNNEL:
            backend.delete_tunnel(operation.intent.account_id, response_id)
        case JournalOperationKind.DNS_RECORD:
            if operation.intent.zone_id is None:
                raise Task1ProofContextError("Task 1 Cloudflare DNS cleanup scope is invalid")
            backend.delete_dns_record(operation.intent.zone_id, response_id)
        case JournalOperationKind.ACCESS_APPLICATION:
            backend.delete_access_application(operation.intent.account_id, response_id)
        case JournalOperationKind.ACCESS_POLICY:
            if operation.intent.parent_id is None:
                raise Task1ProofContextError("Task 1 Cloudflare policy cleanup scope is invalid")
            backend.delete_access_policy(
                operation.intent.account_id, operation.intent.parent_id, response_id
            )
        case JournalOperationKind.TUNNEL_CONFIGURATION:
            raise Task1ProofContextError(
                "Task 1 Cloudflare configuration has no standalone resource"
            )
        case unreachable:
            raise AssertionError(f"unexpected journal kind: {unreachable}")


def _canonical_receipt(receipt: dict[str, str]) -> bytes:
    return ("{" + ",".join(f'"{key}":"{receipt[key]}"' for key in sorted(receipt)) + "}\n").encode()
