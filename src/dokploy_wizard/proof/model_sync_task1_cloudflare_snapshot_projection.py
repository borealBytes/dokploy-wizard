"""Redacted resource projections for complete Cloudflare snapshots."""

from __future__ import annotations

from collections.abc import Mapping

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    application_fingerprint,
    configuration_fingerprint,
    dns_fingerprint,
    opaque_hash,
    policy_fingerprint,
    snapshot_value_hash,
    tunnel_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotBackend,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotResourceV1,
)


def project_resources(
    backend: CloudflareSnapshotBackend,
    account_id: str,
    tunnels: tuple[CloudflareTunnel, ...],
    dns: tuple[CloudflareDnsRecord, ...],
    providers: tuple[CloudflareAccessIdentityProvider, ...],
    apps: tuple[CloudflareAccessApplication, ...],
    policies: tuple[CloudflareAccessPolicy, ...],
    certificates: tuple[CloudflareCertificatePack, ...],
) -> tuple[CloudflareSnapshotResourceV1, ...]:
    """Hash all raw provider values before snapshot persistence."""
    return (
        *(_tunnel(backend, account_id, item) for item in tunnels),
        *(_dns(item) for item in dns),
        *(_provider(item) for item in providers),
        *(_application(item) for item in apps),
        *(_policy(item) for item in policies),
        *(_certificate(item) for item in certificates),
    )


def select_otp(
    providers: tuple[CloudflareAccessIdentityProvider, ...],
) -> CloudflareSnapshotResourceV1:
    """Require exactly one external compatible OTP provider."""
    matches = tuple(
        item
        for item in providers
        if item.name == "One-time PIN login" and item.provider_type == "onetimepin"
    )
    if len(matches) != 1:
        from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
            CloudflareSnapshotCollectionError,
        )

        raise CloudflareSnapshotCollectionError("Cloudflare OTP provider is absent or ambiguous")
    return _provider(matches[0])


def _tunnel(
    backend: CloudflareSnapshotBackend, account_id: str, item: CloudflareTunnel
) -> CloudflareSnapshotResourceV1:
    if item.config_src not in {"cloudflare", "local"} or item.status not in {
        "inactive",
        "degraded",
        "healthy",
        "down",
    }:
        from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
            CloudflareSnapshotCollectionError,
        )

        raise CloudflareSnapshotCollectionError("Cloudflare tunnel semantics are unsupported")
    try:
        configuration = (
            configuration_fingerprint(
                tuple(
                    dict(value)
                    for value in backend.get_tunnel_configuration(account_id, item.tunnel_id)
                )
            )
            if item.config_src == "cloudflare"
            else _hash("local-configuration-unavailable")
        )
    except (TypeError, ValueError) as error:
        from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
            CloudflareSnapshotCollectionError,
        )

        raise CloudflareSnapshotCollectionError(
            "Cloudflare tunnel configuration is malformed"
        ) from error
    return _resource(
        "tunnel",
        item.tunnel_id,
        {
            "configuration_sha256": configuration,
            "configuration_availability": "read"
            if item.config_src == "cloudflare"
            else "unavailable_local",
            "config_src": item.config_src,
            "name_sha256": _hash(item.name),
            "spec_sha256": tunnel_fingerprint(item),
            "status": item.status,
        },
    )


def _dns(item: CloudflareDnsRecord) -> CloudflareSnapshotResourceV1:
    return _resource(
        "dns_record",
        item.record_id,
        {
            "content_sha256": _hash(item.content),
            "name_sha256": _hash(item.name),
            "proxied": item.proxied,
            "record_type": item.record_type,
            "spec_sha256": dns_fingerprint(item),
        },
    )


def _provider(item: CloudflareAccessIdentityProvider) -> CloudflareSnapshotResourceV1:
    return _resource(
        "identity_provider",
        item.provider_id,
        {
            "name_sha256": _hash(item.name),
            "provider_type": item.provider_type,
            "spec_sha256": opaque_hash({"name": item.name, "provider_type": item.provider_type}),
        },
    )


def _application(item: CloudflareAccessApplication) -> CloudflareSnapshotResourceV1:
    return _resource(
        "access_application",
        item.app_id,
        {
            "allowed_identity_provider_sha256": [
                _hash(value) for value in sorted(item.allowed_identity_provider_ids)
            ],
            "app_type": item.app_type,
            "domain_sha256": _hash(item.domain),
            "name_sha256": _hash(item.name),
            "spec_sha256": application_fingerprint(item),
        },
    )


def _policy(item: CloudflareAccessPolicy) -> CloudflareSnapshotResourceV1:
    return _resource(
        "access_policy",
        item.policy_id,
        {
            "app_id_sha256": _hash(item.app_id),
            "decision": item.decision,
            "email_sha256": [_hash(value) for value in sorted(item.emails)],
            "name_sha256": _hash(item.name),
            "spec_sha256": policy_fingerprint(item),
        },
    )


def _certificate(item: CloudflareCertificatePack) -> CloudflareSnapshotResourceV1:
    return _resource(
        "certificate_pack",
        item.pack_id,
        {
            "host_sha256": sorted({_hash(value) for value in item.hosts}),
            "pack_type": item.pack_type,
            "spec_sha256": opaque_hash(
                {"hosts": sorted(item.hosts), "pack_type": item.pack_type, "status": item.status}
            ),
            "status": item.status,
        },
    )


def _resource(
    kind: str, identifier: str, payload: Mapping[str, JsonValue]
) -> CloudflareSnapshotResourceV1:
    return CloudflareSnapshotResourceV1(kind, _hash(identifier), payload)


def _hash(value: str) -> str:
    return snapshot_value_hash(value)
