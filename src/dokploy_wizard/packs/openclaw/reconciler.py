"""OpenClaw/My Farm runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from pathlib import Path
from typing import Mapping, Protocol
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest
from urllib.parse import urlsplit

import ssl

from dokploy_wizard.packs.openclaw.models import (
    OpenClawHealthCheck,
    OpenClawManagedResource,
    OpenClawPhase,
    OpenClawResourceRecord,
    OpenClawResult,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput

OPENCLAW_SERVICE_RESOURCE_TYPE = "openclaw_service"
OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE = "openclaw_mem0_service"
OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE = "openclaw_qdrant_service"
OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE = "openclaw_runtime_service"
MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE = "my_farm_advisor_service"
_PACK_NAMES = ("openclaw", "my-farm-advisor")
_RESOURCE_TYPES = {
    "openclaw": OPENCLAW_SERVICE_RESOURCE_TYPE,
    "my-farm-advisor": MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
}
_TEMPLATE_PATHS = {
    "openclaw": "openclaw.compose.yaml",
    "my-farm-advisor": "my-farm-advisor.compose.yaml",
}


def _migration_required_collision_message(*, pack_name: str, service_name: str) -> str:
    return (
        f"Service name collision detected for '{service_name}'. "
        "Refusing to adopt an unowned existing runtime resource. "
        f"Manual {pack_name} state requires migration into wizard-managed ownership "
        "before install, rerun, or modify can continue."
    )


class OpenClawError(RuntimeError):
    """Raised when OpenClaw/My Farm runtime reconciliation fails or detects drift."""


class OpenClawBackend(Protocol):
    def get_service(self, resource_id: str) -> OpenClawResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> OpenClawResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        template_path: Path,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        template_path: Path,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord: ...

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool: ...


class ShellOpenClawBackend:
    """Deterministic default backend for OpenClaw/My Farm planning and health checks."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        self._forced_existing_service_id = values.get("OPENCLAW_MOCK_EXISTING_SERVICE_ID")
        self._forced_existing_replicas = _optional_positive_int(
            values, "OPENCLAW_MOCK_EXISTING_REPLICAS"
        )
        self._forced_health = _optional_bool(values, "OPENCLAW_MOCK_HEALTHY")
        self._service: OpenClawResourceRecord | None = None

    def get_service(self, resource_id: str) -> OpenClawResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        if self._forced_existing_service_id == resource_id:
            return OpenClawResourceRecord(
                resource_id=resource_id,
                resource_name=resource_id,
                replicas=self._forced_existing_replicas or 1,
            )
        return OpenClawResourceRecord(
            resource_id=resource_id,
            resource_name=resource_id,
            replicas=self._forced_existing_replicas or 1,
        )

    def find_service_by_name(self, resource_name: str) -> OpenClawResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        if self._forced_existing_service_id is None:
            return None
        return OpenClawResourceRecord(
            resource_id=self._forced_existing_service_id,
            resource_name=resource_name,
            replicas=self._forced_existing_replicas or 1,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        template_path: Path,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, secret_refs
        self._service = OpenClawResourceRecord(
            resource_id=resource_name,
            resource_name=resource_name,
            replicas=replicas,
        )
        return self._service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        template_path: Path,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, secret_refs
        self._service = OpenClawResourceRecord(
            resource_id=resource_id,
            resource_name=resource_name,
            replicas=replicas,
        )
        return self._service

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool:
        del service
        if self._forced_health is not None:
            return self._forced_health
        if _http_health_check(url):
            return True
        return _local_https_health_check(url)


def reconcile_openclaw(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: OpenClawBackend,
) -> OpenClawPhase:
    return _reconcile_advisor_pack(
        pack_name="openclaw",
        dry_run=dry_run,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        backend=backend,
    )


def reconcile_my_farm_advisor(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: OpenClawBackend,
) -> OpenClawPhase:
    return _reconcile_advisor_pack(
        pack_name="my-farm-advisor",
        dry_run=dry_run,
        desired_state=desired_state,
        ownership_ledger=ownership_ledger,
        backend=backend,
    )


def _reconcile_advisor_pack(
    *,
    pack_name: str,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: OpenClawBackend,
) -> OpenClawPhase:
    if pack_name not in desired_state.enabled_packs:
        return OpenClawPhase(
            result=OpenClawResult(
                outcome="skipped",
                enabled=False,
                variant=None,
                hostname=None,
                channels=(),
                replicas=None,
                template_path=None,
                service=None,
                secret_refs=(),
                health_check=None,
                notes=(f"{pack_name} is not enabled for this install.",),
            ),
            service_resource_id=None,
        )

    hostname = desired_state.hostnames.get(pack_name)
    if hostname is None:
        raise OpenClawError(
            f"Desired state is missing the canonical hostname at hostnames['{pack_name}']."
        )

    template_path = _resolve_template_path(pack_name)
    service_name = _service_name(desired_state.stack_name, pack_name)
    secret_refs: tuple[str, ...] = ()
    health_url = f"https://{hostname}{_health_path_for_pack(pack_name)}"
    channels = _channels_for_pack(desired_state, pack_name)
    replicas = _replicas_for_pack(desired_state, pack_name) or 1

    service, service_id = _resolve_service(
        dry_run=dry_run,
        pack_name=pack_name,
        service_name=service_name,
        hostname=hostname,
        template_path=template_path,
        variant=pack_name,
        channels=channels,
        replicas=replicas,
        secret_refs=secret_refs,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    if dry_run:
        return OpenClawPhase(
            result=OpenClawResult(
                outcome="plan_only",
                enabled=True,
                variant=pack_name,
                hostname=hostname,
                channels=channels,
                replicas=replicas,
                template_path=str(template_path),
                service=service,
                secret_refs=secret_refs,
                health_check=OpenClawHealthCheck(url=health_url, passed=None),
                notes=(
                    f"Advisor pack '{pack_name}' will be reconciled from '{template_path.name}'.",
                    f"{pack_name} success in non-dry-run mode is gated on a backend health check.",
                ),
            ),
            service_resource_id=None,
        )

    service_record = OpenClawResourceRecord(
        resource_id=service_id,
        resource_name=service.resource_name,
        replicas=replicas,
    )
    health_passed = backend.check_health(service=service_record, url=health_url)
    if not health_passed:
        raise OpenClawError(f"Advisor health check failed for '{health_url}'.")

    return OpenClawPhase(
        result=OpenClawResult(
            outcome="applied"
            if service.action in {"create", "update_owned"}
            else "already_present",
            enabled=True,
            variant=pack_name,
            hostname=hostname,
            channels=channels,
            replicas=replicas,
            template_path=str(template_path),
            service=service,
            secret_refs=secret_refs,
            health_check=OpenClawHealthCheck(url=health_url, passed=True),
            notes=(
                f"Advisor pack '{pack_name}' is reconciled and healthy.",
                f"{pack_name} ownership is scoped to its own runtime resource.",
            ),
        ),
        service_resource_id=service_id,
    )


def build_openclaw_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    service_resource_id: str | None,
    nexa_sidecars_enabled: bool,
) -> OwnershipLedger:
    ledger = _build_advisor_ledger(
        existing_ledger=existing_ledger,
        stack_name=stack_name,
        pack_name="openclaw",
        service_resource_id=service_resource_id,
    )
    resources = list(ledger.resources)
    sidecars = (
        (OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE, _nexa_sidecar_scope(stack_name, "mem0")),
        (OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE, _nexa_sidecar_scope(stack_name, "qdrant")),
        (OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE, _nexa_sidecar_scope(stack_name, "nexa-runtime")),
    )
    for resource_type, scope in sidecars:
        resources = [
            resource
            for resource in resources
            if not (resource.resource_type == resource_type and resource.scope == scope)
        ]
        if nexa_sidecars_enabled:
            resources.append(
                OwnedResource(
                    resource_type=resource_type,
                    resource_id=scope,
                    scope=scope,
                )
            )
    return OwnershipLedger(
        format_version=ledger.format_version,
        resources=tuple(resources),
    )


def openclaw_nexa_sidecars_enabled(values: Mapping[str, str]) -> bool:
    return any(
        key.startswith("OPENCLAW_NEXA_") and value.strip() != ""
        for key, value in values.items()
    )


def build_my_farm_advisor_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    service_resource_id: str | None,
) -> OwnershipLedger:
    return _build_advisor_ledger(
        existing_ledger=existing_ledger,
        stack_name=stack_name,
        pack_name="my-farm-advisor",
        service_resource_id=service_resource_id,
    )


def _build_advisor_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    pack_name: str,
    service_resource_id: str | None,
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if not (
            resource.resource_type == _resource_type_for_pack(pack_name)
            and resource.scope == _service_scope(stack_name, pack_name)
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=_resource_type_for_pack(pack_name),
                resource_id=service_resource_id,
                scope=_service_scope(stack_name, pack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _resolve_template_path(pack_name: str) -> Path:
    try:
        template_name = _TEMPLATE_PATHS[pack_name]
    except KeyError as error:
        raise OpenClawError(f"Unsupported advisor pack '{pack_name}'.") from error
    path = Path(__file__).resolve().parents[4] / "templates" / "packs" / template_name
    if not path.is_file():
        raise OpenClawError(f"Missing advisor compose template for '{pack_name}': {path}.")
    return path


def _resolve_service(
    *,
    dry_run: bool,
    pack_name: str,
    service_name: str,
    hostname: str,
    template_path: Path,
    variant: str,
    channels: tuple[str, ...],
    replicas: int,
    secret_refs: tuple[str, ...],
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: OpenClawBackend,
) -> tuple[OpenClawManagedResource, str]:
    owned_resource = _find_owned_resource(ownership_ledger, stack_name, pack_name)
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise OpenClawError(
                f"Ownership ledger says the {pack_name} service exists, "
                "but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise OpenClawError(
                f"Ownership ledger {pack_name} service no longer matches "
                "the desired naming convention."
            )
        if dry_run:
            action = "update_owned" if existing.replicas != replicas else "reuse_owned"
            return (
                OpenClawManagedResource(
                    action=action,
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        updated = backend.update_service(
            resource_id=existing.resource_id,
            resource_name=service_name,
            hostname=hostname,
            template_path=template_path,
            variant=variant,
            channels=channels,
            replicas=replicas,
            secret_refs=secret_refs,
        )
        return (
            OpenClawManagedResource(
                action="update_owned",
                resource_id=updated.resource_id,
                resource_name=updated.resource_name,
            ),
            updated.resource_id,
        )

    existing = backend.find_service_by_name(service_name)
    if existing is not None:
        if existing.resource_id.startswith("dokploy-compose:"):
            if not dry_run:
                updated = backend.update_service(
                    resource_id=existing.resource_id,
                    resource_name=service_name,
                    hostname=hostname,
                    template_path=template_path,
                    variant=variant,
                    channels=channels,
                    replicas=replicas,
                    secret_refs=secret_refs,
                )
                return (
                    OpenClawManagedResource(
                        action="reuse_existing",
                        resource_id=updated.resource_id,
                        resource_name=updated.resource_name,
                    ),
                    updated.resource_id,
                )
            return (
                OpenClawManagedResource(
                    action="reuse_existing",
                    resource_id=existing.resource_id,
                    resource_name=existing.resource_name,
                ),
                existing.resource_id,
            )
        raise OpenClawError(
            _migration_required_collision_message(
                pack_name=pack_name,
                service_name=service_name,
            )
        )

    if dry_run:
        planned_id = f"planned:{service_name}"
        return (
            OpenClawManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=service_name,
            ),
            planned_id,
        )

    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        template_path=template_path,
        variant=variant,
        channels=channels,
        replicas=replicas,
        secret_refs=secret_refs,
    )
    return (
        OpenClawManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _find_owned_resource(
    ownership_ledger: OwnershipLedger, stack_name: str, pack_name: str
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == _resource_type_for_pack(pack_name)
        and resource.scope == _service_scope(stack_name, pack_name)
    ]
    if len(matches) > 1:
        scope = _service_scope(stack_name, pack_name)
        raise OpenClawError(f"Ownership ledger contains multiple services for scope '{scope}'.")
    return matches[0] if matches else None


def _service_name(stack_name: str, pack_name: str) -> str:
    suffix = "openclaw" if pack_name == "openclaw" else "my-farm-advisor"
    return f"{stack_name}-{suffix}"


def _service_scope(stack_name: str, pack_name: str) -> str:
    return f"stack:{stack_name}:{pack_name}"


def _nexa_sidecar_scope(stack_name: str, service_name: str) -> str:
    return f"stack:{stack_name}:openclaw-sidecar:{service_name}"


def _resource_type_for_pack(pack_name: str) -> str:
    try:
        return _RESOURCE_TYPES[pack_name]
    except KeyError as error:
        raise OpenClawError(f"Unsupported pack '{pack_name}'.") from error


def _channels_for_pack(desired_state: DesiredState, pack_name: str) -> tuple[str, ...]:
    if pack_name == "openclaw":
        return desired_state.openclaw_channels
    if pack_name == "my-farm-advisor":
        return desired_state.my_farm_advisor_channels
    raise OpenClawError(f"Unsupported pack '{pack_name}'.")


def _replicas_for_pack(desired_state: DesiredState, pack_name: str) -> int | None:
    if pack_name == "openclaw":
        return desired_state.openclaw_replicas
    if pack_name == "my-farm-advisor":
        return desired_state.my_farm_advisor_replicas
    raise OpenClawError(f"Unsupported pack '{pack_name}'.")


def _health_path_for_pack(pack_name: str) -> str:
    if pack_name == "my-farm-advisor":
        return "/healthz"
    if pack_name == "openclaw":
        return "/health"
    raise OpenClawError(f"Unsupported pack '{pack_name}'.")


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise OpenClawError(f"Invalid boolean value for '{key}': {raw_value!r}.")


def _optional_positive_int(values: dict[str, str], key: str) -> int | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    try:
        parsed = int(raw_value)
    except ValueError as error:
        raise OpenClawError(
            f"Invalid positive integer value for '{key}': {raw_value!r}."
        ) from error
    if parsed < 1:
        raise OpenClawError(f"Invalid positive integer value for '{key}': {raw_value!r}.")
    return parsed


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlsplit(url)
    if not parsed.hostname:
        return False
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    req = urlrequest.Request(
        f"https://127.0.0.1{request_path}",
        headers={"Host": parsed.hostname},
        method="GET",
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urlrequest.urlopen(req, timeout=15, context=context):  # noqa: S310
            return True
    except urlerror.HTTPError:
        return False
    except (urlerror.URLError, TimeoutError):
        return False


def _http_health_check(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    parsed = urlsplit(url)
    host = parsed.netloc
    path = parsed.path or "/health"
    connection: http.client.HTTPSConnection | None = None
    try:
        connection = http.client.HTTPSConnection(host, timeout=2.0)
        connection.request("GET", path)
        response = connection.getresponse()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()
