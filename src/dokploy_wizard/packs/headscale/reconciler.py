"""Headscale runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
import ssl
from typing import Protocol
from urllib import parse

from dokploy_wizard.packs.headscale.models import (
    HeadscaleHealthCheck,
    HeadscaleManagedResource,
    HeadscalePhase,
    HeadscaleResourceRecord,
    HeadscaleResult,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput

HEADSCALE_SERVICE_RESOURCE_TYPE = "headscale_service"


class HeadscaleError(RuntimeError):
    """Raised when Headscale reconciliation fails or detects drift."""


class HeadscaleBackend(Protocol):
    def get_service(self, resource_id: str) -> HeadscaleResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> HeadscaleResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
    ) -> HeadscaleResourceRecord: ...

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool: ...


class ShellHeadscaleBackend:
    """Deterministic default backend for Headscale runtime planning and health checks."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        self._forced_existing_service_id = values.get("HEADSCALE_MOCK_EXISTING_SERVICE_ID")
        self._forced_health = _optional_bool(values, "HEADSCALE_MOCK_HEALTHY")
        self._service: HeadscaleResourceRecord | None = None

    def get_service(self, resource_id: str) -> HeadscaleResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        if self._forced_existing_service_id == resource_id:
            return HeadscaleResourceRecord(resource_id=resource_id, resource_name=resource_id)
        return HeadscaleResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> HeadscaleResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        if self._forced_existing_service_id is None:
            return None
        return HeadscaleResourceRecord(
            resource_id=self._forced_existing_service_id,
            resource_name=resource_name,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        secret_refs: tuple[str, ...],
    ) -> HeadscaleResourceRecord:
        del hostname, secret_refs
        self._service = HeadscaleResourceRecord(
            resource_id=resource_name, resource_name=resource_name
        )
        return self._service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service
        if self._forced_health is not None:
            return self._forced_health
        return _http_health_check(url)


def reconcile_headscale(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: HeadscaleBackend,
) -> HeadscalePhase:
    if "headscale" not in desired_state.enabled_packs:
        return HeadscalePhase(
            result=HeadscaleResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                secret_refs=(),
                health_check=None,
                notes=("Headscale pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
        )

    hostname = desired_state.hostnames.get("headscale")
    if hostname is None:
        raise HeadscaleError(
            "Desired state is missing the canonical Headscale hostname at hostnames['headscale']."
        )

    service_name = _service_name(desired_state.stack_name)
    secret_refs = _secret_refs(desired_state.stack_name)
    health_url = f"https://{hostname}/health"

    service, service_id = _resolve_service(
        dry_run=dry_run,
        service_name=service_name,
        hostname=hostname,
        secret_refs=secret_refs,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    if dry_run:
        return HeadscalePhase(
            result=HeadscaleResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                secret_refs=secret_refs,
                health_check=HeadscaleHealthCheck(url=health_url, passed=None),
                notes=(
                    f"Headscale service '{service_name}' will be exposed at '{hostname}'.",
                    "Headscale success in non-dry-run mode is gated on a backend health check.",
                ),
            ),
            service_resource_id=None,
        )

    service_record = HeadscaleResourceRecord(
        resource_id=service_id, resource_name=service.resource_name
    )
    health_passed = backend.check_health(service=service_record, url=health_url)
    if not health_passed:
        raise HeadscaleError(f"Headscale health check failed for '{health_url}'.")

    return HeadscalePhase(
        result=HeadscaleResult(
            outcome="applied" if service.action == "create" else "already_present",
            enabled=True,
            hostname=hostname,
            service=service,
            secret_refs=secret_refs,
            health_check=HeadscaleHealthCheck(url=health_url, passed=True),
            notes=(
                f"Headscale service '{service_name}' is reconciled and healthy.",
                "Headscale secret refs are deterministic names only; "
                "secret values are not persisted.",
            ),
        ),
        service_resource_id=service_id,
    )


def build_headscale_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    service_resource_id: str | None,
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if not (
            resource.resource_type == HEADSCALE_SERVICE_RESOURCE_TYPE
            and resource.scope == _service_scope(stack_name)
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=HEADSCALE_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _resolve_service(
    *,
    dry_run: bool,
    service_name: str,
    hostname: str,
    secret_refs: tuple[str, ...],
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: HeadscaleBackend,
) -> tuple[HeadscaleManagedResource, str]:
    owned_resource = _find_owned_resource(ownership_ledger, stack_name)
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise HeadscaleError(
                "Ownership ledger says the Headscale service exists, "
                "but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise HeadscaleError(
                "Ownership ledger Headscale service no longer matches "
                "the desired naming convention."
            )
        return (
            HeadscaleManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    existing = backend.find_service_by_name(service_name)
    if existing is not None:
        existing = backend.create_service(
            resource_name=service_name,
            hostname=hostname,
            secret_refs=secret_refs,
        )
        return (
            HeadscaleManagedResource(
                action="reuse_existing",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    if dry_run:
        planned_id = f"planned:{service_name}"
        return (
            HeadscaleManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=service_name,
            ),
            planned_id,
        )

    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        secret_refs=secret_refs,
    )
    return (
        HeadscaleManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _find_owned_resource(
    ownership_ledger: OwnershipLedger, stack_name: str
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == HEADSCALE_SERVICE_RESOURCE_TYPE
        and resource.scope == _service_scope(stack_name)
    ]
    if len(matches) > 1:
        scope = _service_scope(stack_name)
        raise HeadscaleError(
            f"Ownership ledger contains multiple Headscale services for scope '{scope}'."
        )
    return matches[0] if matches else None


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-headscale"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:headscale"


def _secret_refs(stack_name: str) -> tuple[str, ...]:
    return (
        f"{stack_name}-headscale-admin-api-key",
        f"{stack_name}-headscale-noise-private-key",
    )


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise HeadscaleError(f"Invalid boolean value for '{key}': {raw_value!r}.")


def _http_health_check(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    parsed = parse.urlparse(url)
    host = parsed.netloc
    if _https_health_request(host=host, path="/health"):
        return True
    return _https_health_request(
        host="127.0.0.1",
        path="/health",
        headers={"Host": host},
        insecure=True,
    )


def _https_health_request(
    *,
    host: str,
    path: str,
    headers: dict[str, str] | None = None,
    insecure: bool = False,
) -> bool:
    connection: http.client.HTTPSConnection | None = None
    try:
        context = ssl._create_unverified_context() if insecure else None
        connection = http.client.HTTPSConnection(host, timeout=2.0, context=context)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()
