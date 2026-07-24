"""Crash-safe Task 1 tunnel-configuration reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    configuration_fingerprint,
    operation_key,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    JournalOperationState,
    Task1CloudflareJournal,
    Task1CloudflareJournalIntent,
)


class Task1TunnelConfigurationBackend(Protocol):
    """Minimal provider capability required by configuration reconciliation."""

    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[dict[str, object], ...]: ...

    def update_tunnel_configuration(
        self, account_id: str, tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Task1TunnelConfigurationRequest:
    """Exact account, tunnel, and desired ingress bound into one journal intent."""

    account_id: str
    tunnel_id: str
    ingress: tuple[dict[str, object], ...]


def reconcile_task1_tunnel_configuration(
    *,
    journal: Task1CloudflareJournal,
    backend: Task1TunnelConfigurationBackend,
    request: Task1TunnelConfigurationRequest,
    error_type: type[Exception],
) -> None:
    """Update only when the observed configuration is the durable pre-image or desired state."""
    desired = configuration_fingerprint(request.ingress)
    scope = {"account_id": request.account_id, "tunnel_id": request.tunnel_id}
    logical_key = operation_key(JournalOperationKind.TUNNEL_CONFIGURATION, scope)
    operation = journal.existing_operation_by_logical_key(logical_key)
    if operation is None:
        pre_image = backend.get_tunnel_configuration(request.account_id, request.tunnel_id)
        operation = journal.record_intent(
            Task1CloudflareJournalIntent(
                logical_key=logical_key,
                kind=JournalOperationKind.TUNNEL_CONFIGURATION,
                account_id=request.account_id,
                zone_id=None,
                parent_id=request.tunnel_id,
                expected_name_sha256=None,
                expected_domain_sha256=None,
                pre_absence_sha256=configuration_fingerprint(pre_image),
                desired_spec_sha256=desired,
            )
        )
    _validate_configuration_intent(operation.intent, request, desired, error_type)
    current = backend.get_tunnel_configuration(request.account_id, request.tunnel_id)
    current_fingerprint = configuration_fingerprint(current)
    if operation.state is JournalOperationState.CREATED:
        if current_fingerprint != desired or operation.post_fingerprint_sha256 != desired:
            raise error_type("Task 1 Cloudflare tunnel configuration recovery drifted")
        return
    if operation.state is not JournalOperationState.INTENT:
        raise error_type("Task 1 Cloudflare tunnel configuration intent state drifted")
    if current_fingerprint == desired:
        journal.checkpoint_created(operation, response_id=None, post_fingerprint_sha256=desired)
        return
    if current_fingerprint != operation.intent.pre_absence_sha256:
        raise error_type("Task 1 Cloudflare tunnel configuration recovery drifted")
    backend.update_tunnel_configuration(request.account_id, request.tunnel_id, request.ingress)
    journal.record_provider_response(operation)
    observed = backend.get_tunnel_configuration(request.account_id, request.tunnel_id)
    if configuration_fingerprint(observed) != desired:
        raise error_type("Task 1 Cloudflare tunnel configuration response drifted")
    journal.checkpoint_created(operation, response_id=None, post_fingerprint_sha256=desired)


def _validate_configuration_intent(
    intent: Task1CloudflareJournalIntent,
    request: Task1TunnelConfigurationRequest,
    desired: str,
    error_type: type[Exception],
) -> None:
    """Reject every replay shape other than the sole canonical configuration intent."""
    if (
        intent.kind is not JournalOperationKind.TUNNEL_CONFIGURATION
        or intent.account_id != request.account_id
        or intent.zone_id is not None
        or intent.parent_id != request.tunnel_id
        or intent.expected_name_sha256 is not None
        or intent.expected_domain_sha256 is not None
        or intent.desired_spec_sha256 != desired
    ):
        raise error_type("Task 1 Cloudflare tunnel configuration intent drifted")
