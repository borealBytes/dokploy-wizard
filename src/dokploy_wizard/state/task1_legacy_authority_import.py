"""Atomic publication of V3-proven legacy uninstall authority."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, assert_never

from dokploy_wizard.core import SHARED_NETWORK_RESOURCE_TYPE
from dokploy_wizard.networking import (
    ACCESS_APPLICATION_RESOURCE_TYPE,
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
    ACCESS_POLICY_RESOURCE_TYPE,
    DNS_RESOURCE_TYPE,
    TUNNEL_RESOURCE_TYPE,
)
from dokploy_wizard.state.models import OwnedResource
from dokploy_wizard.state.task1_legacy_authority_proof import (
    Task1LegacyAuthorityBundle,
    Task1LegacyAuthorityImportError,
    parse_task1_legacy_authority_bundle,
)
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.state.uninstall_authority_schema import (
    UninstallAuthority,
    UninstallAuthorityError,
)
from dokploy_wizard.state.uninstall_provenance import (
    ProviderCreationDisposition,
    ProviderCreationResult,
)
from dokploy_wizard.uninstall.families import ProviderFamily, family_for

_MAX_LEDGER_BYTES: Final = 4 * 1024 * 1024
_CLOUDFLARE_PROVIDERS: Final = {
    TUNNEL_RESOURCE_TYPE: "cloudflare",
    DNS_RESOURCE_TYPE: "cloudflare_dns",
    ACCESS_OTP_PROVIDER_RESOURCE_TYPE: "cloudflare_access_identity_provider",
    ACCESS_APPLICATION_RESOURCE_TYPE: "cloudflare_access_application",
    ACCESS_POLICY_RESOURCE_TYPE: "cloudflare_access_policy",
}


@dataclass(frozen=True, slots=True)
class Task1LegacyAuthorityImportRequest:
    """Proof bytes and trusted in-memory creation observations for one import."""

    bundle: Task1LegacyAuthorityBundle
    observations: tuple[ProviderCreationResult, ...]


def import_task1_legacy_authority(
    state_dir: Path,
    current_ledger_path: Path,
    request: Task1LegacyAuthorityImportRequest,
) -> tuple[UninstallAuthority, ...]:
    """Validate the complete import before atomically publishing any receipt."""

    proven = parse_task1_legacy_authority_bundle(request.bundle)
    if _read_current_ledger(current_ledger_path) != request.bundle.ownership_ledger_bytes:
        raise Task1LegacyAuthorityImportError("Task 1 current ownership ledger bytes drifted.")
    authorities = _validated_authorities(proven.ownership_ledger.resources, request.observations)
    try:
        return UninstallAuthorityStore(state_dir).record_created_batch(authorities)
    except UninstallAuthorityError as error:
        raise Task1LegacyAuthorityImportError(
            "Task 1 preexisting authority receipt drifted."
        ) from error


def _validated_authorities(
    resources: tuple[OwnedResource, ...], observations: tuple[ProviderCreationResult, ...]
) -> tuple[UninstallAuthority, ...]:
    ledger = {_resource_key(resource): resource for resource in resources}
    observed_keys = tuple(_resource_key(observation.resource) for observation in observations)
    if len(set(observed_keys)) != len(observed_keys) or set(observed_keys) != set(ledger):
        raise Task1LegacyAuthorityImportError(
            "Task 1 provider observations must cover the ledger exactly once."
        )
    authorities: list[UninstallAuthority] = []
    for observation in observations:
        resource = ledger[_resource_key(observation.resource)]
        if observation.resource != resource:
            raise Task1LegacyAuthorityImportError("Task 1 provider observation resource drifted.")
        match observation.disposition:
            case ProviderCreationDisposition.CREATED:
                pass
            case ProviderCreationDisposition.REUSED | ProviderCreationDisposition.UPDATED:
                raise Task1LegacyAuthorityImportError(
                    "Task 1 authority requires exact CREATED provider observations."
                )
            case unexpected:
                assert_never(unexpected)
        if observation.provider != _provider_for(resource.resource_type):
            raise Task1LegacyAuthorityImportError(
                "Task 1 provider observation has the wrong provider family."
            )
        authorities.append(
            UninstallAuthority(
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                scope=resource.scope,
                owner_id=observation.owner_id,
                provider=observation.provider,
                physical_target_id=observation.physical_target_id,
                parent_target_id=observation.parent_target_id,
                expected_fingerprint=observation.expected_fingerprint,
            )
        )
    return tuple(authorities)


def _provider_for(resource_type: str) -> str:
    family = family_for(resource_type)
    match family:
        case ProviderFamily.CLOUDFLARE:
            provider = _CLOUDFLARE_PROVIDERS.get(resource_type)
            if provider is None:
                raise Task1LegacyAuthorityImportError(
                    "Task 1 Cloudflare resource has no authority provider."
                )
            return provider
        case ProviderFamily.TAILSCALE:
            return "tailscale"
        case ProviderFamily.DOKPLOY:
            return "dokploy_compose"
        case ProviderFamily.DOCKER:
            if resource_type == SHARED_NETWORK_RESOURCE_TYPE:
                return "docker_network"
            return "docker_volume"
        case ProviderFamily.SCHEDULE:
            raise Task1LegacyAuthorityImportError(
                "Task 1 legacy proof cannot authorize a later sync schedule."
            )
        case unexpected:
            assert_never(unexpected)


def _resource_key(resource: OwnedResource) -> tuple[str, str, str]:
    return resource.resource_type, resource.resource_id, resource.scope


def _read_current_ledger(path: Path) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise Task1LegacyAuthorityImportError(
            "Task 1 current ownership ledger is absent."
        ) from error
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_LEDGER_BYTES:
        raise Task1LegacyAuthorityImportError("Task 1 current ownership ledger is not bounded.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        content = bytearray()
        while len(content) <= opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
        raise Task1LegacyAuthorityImportError("Task 1 current ownership ledger changed.")
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise Task1LegacyAuthorityImportError("Task 1 current ownership ledger changed.")
    if identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
        raise Task1LegacyAuthorityImportError("Task 1 current ownership ledger changed.")
    if len(content) != before.st_size:
        raise Task1LegacyAuthorityImportError(
            "Task 1 current ownership ledger read was incomplete."
        )
    return bytes(content)
