"""Fail-closed journal-to-snapshot delta validation for Task 1."""

from __future__ import annotations

from typing import Final, assert_never

from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import snapshot_value_hash
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalOperationKind,
    JournalOperationState,
    JournalStatus,
    Task1CloudflareJournalDocument,
    Task1CloudflareJournalOperation,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotResourceV1,
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema_types import (
    CloudflareSnapshotError,
)

_RESOURCE_KIND: Final = {
    JournalOperationKind.TUNNEL: "tunnel",
    JournalOperationKind.DNS_RECORD: "dns_record",
    JournalOperationKind.ACCESS_APPLICATION: "access_application",
    JournalOperationKind.ACCESS_POLICY: "access_policy",
}


class CloudflareSnapshotDeltaError(CloudflareSnapshotError):
    """Raised when a provider snapshot differs from the exact Task 1 journal delta."""


def validate_install_delta(
    pre_install: CloudflareSnapshotV1,
    post_install: CloudflareSnapshotV1,
    journal: Task1CloudflareJournalDocument,
) -> None:
    """Allow only exact journal-created resources and their tunnel configuration."""
    if journal.context_sha256 != pre_install.context_sha256:
        raise CloudflareSnapshotDeltaError("Cloudflare journal is not valid for this snapshot")
    _same_scope(pre_install, post_install)
    if pre_install.otp_provider_sha256 != post_install.otp_provider_sha256:
        raise CloudflareSnapshotDeltaError("Cloudflare OTP provider drifted after install")
    before = _resources(pre_install)
    after = _resources(post_install)
    if any(after.get(key) != value for key, value in before.items()):
        raise CloudflareSnapshotDeltaError("preexisting Cloudflare resource changed after install")
    additions = set(after) - set(before)
    expected = _journal_additions(journal, after)
    if additions != expected:
        raise CloudflareSnapshotDeltaError("Cloudflare snapshot has unjournaled additions")


def _same_scope(before: CloudflareSnapshotV1, after: CloudflareSnapshotV1) -> None:
    if (
        before.context_sha256 != after.context_sha256
        or before.account_id_sha256 != after.account_id_sha256
        or before.zone_id_sha256 != after.zone_id_sha256
    ):
        raise CloudflareSnapshotDeltaError("Cloudflare snapshot scope changed")


def _resources(
    snapshot: CloudflareSnapshotV1,
) -> dict[tuple[str, str], CloudflareSnapshotResourceV1]:
    return {(item.kind, item.identity_sha256): item for item in snapshot.resources}


def _journal_additions(
    journal: Task1CloudflareJournalDocument,
    post: dict[tuple[str, str], CloudflareSnapshotResourceV1],
) -> set[tuple[str, str]]:
    _validate_operation_states(journal)
    additions: set[tuple[str, str]] = set()
    configurations: list[Task1CloudflareJournalOperation] = []
    for operation in journal.operations:
        match operation.state:
            case JournalOperationState.CREATED | JournalOperationState.CLEANED:
                if operation.intent.kind is JournalOperationKind.TUNNEL_CONFIGURATION:
                    configurations.append(operation)
                    continue
                additions.add(_created_resource_identity(operation, post))
            case JournalOperationState.ABORTED:
                _reject_aborted_resource(operation, post)
            case JournalOperationState.INTENT:
                raise CloudflareSnapshotDeltaError("Cloudflare journal operation is incomplete")
            case unreachable:
                assert_never(unreachable)
    for operation in configurations:
        parent = operation.intent.parent_id
        if parent is None or operation.post_fingerprint_sha256 is None:
            raise CloudflareSnapshotDeltaError("Cloudflare configuration journal is incomplete")
        resource = post.get(("tunnel", _opaque(parent)))
        if (
            resource is None
            or resource.payload["configuration_sha256"] != operation.post_fingerprint_sha256
        ):
            raise CloudflareSnapshotDeltaError(
                "Cloudflare tunnel configuration does not match snapshot"
            )
    return additions


def _validate_operation_states(journal: Task1CloudflareJournalDocument) -> None:
    match journal.status:
        case JournalStatus.ACTIVE:
            allowed = frozenset(
                {
                    JournalOperationState.CREATED,
                    JournalOperationState.CLEANED,
                    JournalOperationState.ABORTED,
                }
            )
        case JournalStatus.CLEANED:
            allowed = frozenset({JournalOperationState.CLEANED})
        case JournalStatus.RESTORED:
            allowed = frozenset({JournalOperationState.CLEANED, JournalOperationState.ABORTED})
        case unreachable:
            assert_never(unreachable)
    if any(operation.state not in allowed for operation in journal.operations):
        raise CloudflareSnapshotDeltaError("Cloudflare journal operation state is invalid")


def _created_resource_identity(
    operation: Task1CloudflareJournalOperation,
    post: dict[tuple[str, str], CloudflareSnapshotResourceV1],
) -> tuple[str, str]:
    resource_kind = _RESOURCE_KIND.get(operation.intent.kind)
    if (
        resource_kind is None
        or operation.response_id is None
        or operation.post_fingerprint_sha256 is None
    ):
        raise CloudflareSnapshotDeltaError("Cloudflare journal operation is incomplete")
    identity = (resource_kind, _opaque(operation.response_id))
    resource = post.get(identity)
    if resource is None or resource.payload["spec_sha256"] != operation.post_fingerprint_sha256:
        raise CloudflareSnapshotDeltaError("Cloudflare journal resource does not match snapshot")
    _resource_kind, bindings = _resource_bindings(operation)
    if not _resource_matches_bindings(resource, bindings):
        raise CloudflareSnapshotDeltaError(
            "Cloudflare journal resource identity does not match snapshot"
        )
    return identity


def _reject_aborted_resource(
    operation: Task1CloudflareJournalOperation,
    post: dict[tuple[str, str], CloudflareSnapshotResourceV1],
) -> None:
    resource_kind, bindings = _resource_bindings(operation)
    if resource_kind is None:
        return
    if any(
        resource.kind == resource_kind and _resource_matches_bindings(resource, bindings)
        for resource in post.values()
    ):
        raise CloudflareSnapshotDeltaError("Cloudflare snapshot contains an aborted resource")


def _resource_bindings(
    operation: Task1CloudflareJournalOperation,
) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    intent = operation.intent
    match intent.kind:
        case JournalOperationKind.TUNNEL:
            return "tunnel", _hash_binding("name_sha256", intent.expected_name_sha256)
        case JournalOperationKind.DNS_RECORD:
            return "dns_record", _hash_binding("name_sha256", intent.expected_name_sha256)
        case JournalOperationKind.ACCESS_APPLICATION:
            return "access_application", _hash_bindings(
                ("name_sha256", intent.expected_name_sha256),
                ("domain_sha256", intent.expected_domain_sha256),
            )
        case JournalOperationKind.ACCESS_POLICY:
            return "access_policy", _hash_bindings(
                ("name_sha256", intent.expected_name_sha256),
                ("app_id_sha256", None if intent.parent_id is None else _opaque(intent.parent_id)),
            )
        case JournalOperationKind.TUNNEL_CONFIGURATION:
            return None, ()
        case unreachable:
            assert_never(unreachable)


def _hash_binding(field: str, value: str | None) -> tuple[tuple[str, str], ...]:
    return () if value is None else ((field, value),)


def _hash_bindings(
    first: tuple[str, str | None], second: tuple[str, str | None]
) -> tuple[tuple[str, str], ...]:
    return (*_hash_binding(*first), *_hash_binding(*second))


def _resource_matches_bindings(
    resource: CloudflareSnapshotResourceV1, bindings: tuple[tuple[str, str], ...]
) -> bool:
    return all(resource.payload.get(field) == expected for field, expected in bindings)


def _opaque(value: str) -> str:
    return snapshot_value_hash(value)
