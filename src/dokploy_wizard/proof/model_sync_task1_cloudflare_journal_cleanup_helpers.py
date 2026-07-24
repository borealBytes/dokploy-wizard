"""Pure cleanup ordering and provider fingerprint helpers."""

from __future__ import annotations

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    application_fingerprint,
    dns_fingerprint,
    policy_fingerprint,
    tunnel_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalOperationKind,
    Task1CloudflareJournalOperation,
)


def cleanup_order(operation: Task1CloudflareJournalOperation) -> int:
    """Order children before their proof-created Cloudflare parents."""
    match operation.intent.kind:
        case JournalOperationKind.ACCESS_POLICY:
            return 0
        case JournalOperationKind.ACCESS_APPLICATION:
            return 1
        case JournalOperationKind.DNS_RECORD:
            return 2
        case JournalOperationKind.TUNNEL:
            return 3
        case JournalOperationKind.TUNNEL_CONFIGURATION:
            return 4
        case unreachable:
            raise AssertionError(f"unexpected journal kind: {unreachable}")


def resource_fingerprint(
    resource: (
        CloudflareTunnel
        | CloudflareDnsRecord
        | CloudflareAccessApplication
        | CloudflareAccessPolicy
    ),
) -> str:
    """Fingerprint one provider object before a receipt-owned deletion."""
    match resource:
        case CloudflareTunnel():
            return tunnel_fingerprint(resource)
        case CloudflareDnsRecord():
            return dns_fingerprint(resource)
        case CloudflareAccessApplication():
            return application_fingerprint(resource)
        case CloudflareAccessPolicy():
            return policy_fingerprint(resource)
        case unreachable:
            raise AssertionError(f"unexpected Cloudflare resource: {unreachable}")
