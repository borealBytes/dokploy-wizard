"""Canonical opaque fingerprints for Task 1 Cloudflare journal operations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalOperationKind,
)

if TYPE_CHECKING:
    from dokploy_wizard.networking.cloudflare import (
        CloudflareAccessApplication,
        CloudflareAccessPolicy,
        CloudflareDnsRecord,
        CloudflareTunnel,
    )


def opaque_hash(value: object) -> str:
    """Hash an exact canonical provider projection without writing it to evidence."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def snapshot_value_hash(value: str) -> str:
    """Hash one redacted provider identifier exactly as a snapshot does."""
    return hashlib.sha256(value.encode()).hexdigest()


def valid_provider_identifier(value: str) -> bool:
    """Accept only bounded opaque Cloudflare identifiers before a follow-up read."""
    return re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def operation_key(kind: JournalOperationKind, scope: Mapping[str, str]) -> str:
    """Derive one stable logical key from the complete provider collision scope."""
    return f"{kind.value}:{opaque_hash(dict(sorted(scope.items())))}"


def absence_fingerprint(kind: JournalOperationKind, scope: Mapping[str, str]) -> str:
    """Bind an exact empty collision-domain observation to one journal intent."""
    return opaque_hash({"kind": kind.value, "matches": [], "scope": dict(sorted(scope.items()))})


def tunnel_fingerprint(tunnel: CloudflareTunnel) -> str:
    return opaque_hash({"name": tunnel.name})


def dns_fingerprint(record: CloudflareDnsRecord) -> str:
    return opaque_hash(
        {
            "content": record.content,
            "name": record.name,
            "proxied": record.proxied,
            "record_type": record.record_type,
        }
    )


def application_fingerprint(application: CloudflareAccessApplication) -> str:
    return opaque_hash(
        {
            "allowed_identity_provider_ids": sorted(application.allowed_identity_provider_ids),
            "app_type": application.app_type,
            "domain": application.domain,
            "name": application.name,
        }
    )


def policy_fingerprint(policy: CloudflareAccessPolicy) -> str:
    return opaque_hash(
        {
            "app_id": policy.app_id,
            "decision": policy.decision,
            "emails": sorted(policy.emails),
            "name": policy.name,
        }
    )


def configuration_fingerprint(ingress: tuple[Mapping[str, object], ...]) -> str:
    return opaque_hash({"ingress": ingress})
