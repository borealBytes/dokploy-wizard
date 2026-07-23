"""Cloudflare evidence adapters for the generic preflight identity model."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from dokploy_wizard.proof.model_sync_artifacts import JsonValue, require_mapping, require_text
from dokploy_wizard.proof.model_sync_cloudflare import (
    CloudflareMatch,
    CloudflareProvenance,
    CloudflareResource,
    classify_cloudflare_post_install,
    classify_cloudflare_preflight,
    cloudflare_snapshot_sha256,
    verify_preexisting_cloudflare,
)
from dokploy_wizard.proof.model_sync_env import ProofNamespace


class CloudflareResourceEvidence(Protocol):
    @property
    def resource_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def fingerprint_sha256(self) -> str | None: ...

    @property
    def match(self) -> CloudflareMatch | None: ...

    @property
    def provenance(self) -> CloudflareProvenance | None: ...


Resource = TypeVar("Resource", bound=CloudflareResourceEvidence)


def build_cloudflare_resources(
    values: list[JsonValue],
    namespace: ProofNamespace,
    factory: Callable[[str, str, str, str, CloudflareMatch, CloudflareProvenance], Resource],
) -> tuple[Resource, ...]:
    """Build redacted evidence while retaining names only during parsing."""
    classified = classify_cloudflare_preflight(
        values,
        set(namespace.cloudflare),
        namespace.stack_name,
    )
    raw_names = _cloudflare_names(values)
    return tuple(
        factory(
            item.resource_id,
            raw_names[(item.kind, item.resource_id)],
            item.kind,
            item.fingerprint_sha256,
            item.match,
            item.provenance,
        )
        for item in classified
    )


def verify_cloudflare_resources(
    before: tuple[CloudflareResourceEvidence, ...],
    after: tuple[CloudflareResourceEvidence, ...],
) -> None:
    """Reject a post-install change to a preserve-only Cloudflare object."""
    verify_preexisting_cloudflare(_evidence(before), _evidence(after))


def cloudflare_snapshot_sha256_for_evidence(
    resources: tuple[CloudflareResourceEvidence, ...],
) -> str:
    return cloudflare_snapshot_sha256(_evidence(resources))


def classify_post_install_resources(
    before: tuple[CloudflareResourceEvidence, ...],
    after: tuple[CloudflareResourceEvidence, ...],
    namespace: ProofNamespace,
    factory: Callable[[str, str, str, str, CloudflareMatch, CloudflareProvenance], Resource],
) -> tuple[Resource, ...]:
    """Classify transient post-install names into redacted evidence records."""
    classified = classify_cloudflare_post_install(
        _raw_resources(after), set(namespace.cloudflare), namespace.stack_name, _evidence(before)
    )
    names = _cloudflare_names(_raw_resources(after))
    return tuple(
        factory(
            item.resource_id,
            names[(item.kind, item.resource_id)],
            item.kind,
            item.fingerprint_sha256,
            item.match,
            item.provenance,
        )
        for item in classified
    )


def _cloudflare_names(values: list[JsonValue]) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    for value in values:
        source = require_mapping(value, "Cloudflare resource")
        kind = require_text(source.get("kind"), "Cloudflare kind")
        resource_id = require_text(source.get("id"), "Cloudflare id")
        names[(kind, resource_id)] = require_text(source.get("name"), "Cloudflare name")
    return names


def _evidence(
    resources: tuple[CloudflareResourceEvidence, ...],
) -> tuple[CloudflareResource, ...]:
    values: list[CloudflareResource] = []
    for item in resources:
        if item.fingerprint_sha256 is None or item.match is None or item.provenance is None:
            raise ValueError("Cloudflare resource evidence is incomplete")
        values.append(
            CloudflareResource(
                item.resource_id,
                item.kind,
                item.fingerprint_sha256,
                item.match,
                item.provenance,
            )
        )
    return tuple(values)


def _raw_resources(resources: tuple[CloudflareResourceEvidence, ...]) -> list[JsonValue]:
    values: list[JsonValue] = []
    for item in resources:
        if item.fingerprint_sha256 is None or item.match is None or item.provenance is None:
            raise ValueError("Cloudflare resource evidence is incomplete")
        values.append(
            {"fingerprint_sha256": item.fingerprint_sha256, "id": item.resource_id,
             "kind": item.kind, "name": item.name}
        )
    return values
