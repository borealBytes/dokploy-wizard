"""Paperclip runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
import os
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from dokploy_wizard.core.models import PackSharedAllocation, SharedPostgresAllocation
from dokploy_wizard.packs.paperclip.models import (
    PaperclipBootstrapState,
    PaperclipHealthState,
    PaperclipPhase,
    PaperclipResourceRecord,
    PaperclipResult,
    PaperclipServiceConfig,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

PAPERCLIP_SERVICE_RESOURCE_TYPE = "paperclip_service"
PAPERCLIP_DATA_RESOURCE_TYPE = "paperclip_data"
_PAPERCLIP_HOME = "/var/lib/paperclip"
_PAPERCLIP_HEALTH_PATH = "/api/health"


class PaperclipError(RuntimeError):
    """Raised when Paperclip reconciliation fails or detects drift."""


class PaperclipBackend(Protocol):
    def get_service(self, resource_id: str) -> PaperclipResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> PaperclipResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> PaperclipResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> PaperclipResourceRecord: ...

    def get_persistent_data(self, resource_id: str) -> PaperclipResourceRecord | None: ...

    def find_persistent_data_by_name(
        self, resource_name: str
    ) -> PaperclipResourceRecord | None: ...

    def create_persistent_data(self, resource_name: str) -> PaperclipResourceRecord: ...

    def check_health(self, *, service: PaperclipResourceRecord, url: str) -> bool: ...

    def ensure_application_ready(
        self, *, better_auth_secret_ref: str
    ) -> PaperclipBootstrapState: ...


class ShellPaperclipBackend:
    def __init__(self) -> None:
        self._service: PaperclipResourceRecord | None = None
        self._data: PaperclipResourceRecord | None = None

    def get_service(self, resource_id: str) -> PaperclipResourceRecord | None:
        if self._service is not None and self._service.resource_id == resource_id:
            return self._service
        return PaperclipResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> PaperclipResourceRecord | None:
        if self._service is not None and self._service.resource_name == resource_name:
            return self._service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> PaperclipResourceRecord:
        del hostname, postgres_service_name, postgres, data_resource_name, env
        self._service = PaperclipResourceRecord(
            resource_id=resource_name,
            resource_name=resource_name,
        )
        return self._service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        data_resource_name: str,
        env: dict[str, str],
    ) -> PaperclipResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_resource_name,
            env=env,
        )

    def get_persistent_data(self, resource_id: str) -> PaperclipResourceRecord | None:
        if self._data is not None and self._data.resource_id == resource_id:
            return self._data
        return PaperclipResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_persistent_data_by_name(self, resource_name: str) -> PaperclipResourceRecord | None:
        if self._data is not None and self._data.resource_name == resource_name:
            return self._data
        return None

    def create_persistent_data(self, resource_name: str) -> PaperclipResourceRecord:
        self._data = PaperclipResourceRecord(resource_id=resource_name, resource_name=resource_name)
        return self._data

    def check_health(self, *, service: PaperclipResourceRecord, url: str) -> bool:
        del service
        return _http_health_check(url)

    def ensure_application_ready(
        self, *, better_auth_secret_ref: str
    ) -> PaperclipBootstrapState:
        del better_auth_secret_ref
        return PaperclipBootstrapState(ready=True)


def reconcile_paperclip(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: PaperclipBackend,
) -> PaperclipPhase:
    if "paperclip" not in desired_state.enabled_packs:
        return PaperclipPhase(
            result=PaperclipResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                service=None,
                health=None,
                bootstrap=None,
                config=None,
                notes=("Paperclip pack is explicitly disabled for this install.",),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    hostname = desired_state.hostnames.get("paperclip")
    if hostname is None:
        raise PaperclipError(
            "Desired state is missing the canonical Paperclip hostname at hostnames['paperclip']."
        )
    if desired_state.access_wrapped_hostnames.get("paperclip") != hostname:
        raise PaperclipError(
            "Paperclip requires authenticated-private browser routing; "
            "local_trusted mode is not supported."
        )

    _, postgres_service_name, postgres = _get_paperclip_allocation(desired_state)
    service_name = _service_name(desired_state.stack_name)
    data_name = _data_name(desired_state.stack_name)
    public_url = f"https://{hostname}"
    health_url = f"{public_url}{_PAPERCLIP_HEALTH_PATH}"
    better_auth_secret_ref = _better_auth_secret_ref(desired_state.stack_name)
    openclaw_gateway_url: str | None = None
    openclaw_gateway_token_secret_ref: str | None = None
    if "openclaw" in desired_state.enabled_packs:
        if desired_state.openclaw_gateway_token is None:
            raise PaperclipError(
                "Paperclip requires an OpenClaw gateway token when the OpenClaw pack is enabled."
            )
        openclaw_gateway_url = _openclaw_gateway_url(desired_state)
        openclaw_gateway_token_secret_ref = _openclaw_gateway_token_secret_ref(
            desired_state.stack_name
        )

    workspace_daemon_base_url: str | None = None
    workspace_daemon_token: str | None = None
    if "paperclip" in desired_state.workspace_daemon_packs:
        workspace_daemon_base_url = _optional_env("WORKSPACE_DAEMON_BASE_URL")
        workspace_daemon_token = _optional_env("WORKSPACE_DAEMON_TOKEN")
        if workspace_daemon_base_url is None or workspace_daemon_token is None:
            raise PaperclipError(
                "Paperclip workspace daemon adapter requires "
                "WORKSPACE_DAEMON_BASE_URL and WORKSPACE_DAEMON_TOKEN when "
                "the workspace daemon is enabled."
            )

    env = _build_env_contract(
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        paperclip_home=_PAPERCLIP_HOME,
        better_auth_secret_ref=better_auth_secret_ref,
        public_url=public_url,
        openclaw_gateway_url=openclaw_gateway_url,
        openclaw_gateway_token_secret_ref=openclaw_gateway_token_secret_ref,
        workspace_daemon_base_url=workspace_daemon_base_url,
        workspace_daemon_token=workspace_daemon_token,
    )

    _, data_id, data_action = _resolve_owned_resource(
        dry_run=dry_run,
        resource_name=data_name,
        resource_type=PAPERCLIP_DATA_RESOURCE_TYPE,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=PAPERCLIP_DATA_RESOURCE_TYPE,
            scope=_data_scope(desired_state.stack_name),
        ),
        get_resource=backend.get_persistent_data,
        find_by_name=backend.find_persistent_data_by_name,
        create_resource=backend.create_persistent_data,
    )
    service, service_id, service_action = _resolve_service(
        dry_run=dry_run,
        service_name=service_name,
        hostname=hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        data_name=data_name,
        env=env,
        ownership_ledger=ownership_ledger,
        stack_name=desired_state.stack_name,
        backend=backend,
    )

    config = PaperclipServiceConfig(
        hostname=hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
    )

    if dry_run:
        return PaperclipPhase(
            result=PaperclipResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                service=service,
                health=PaperclipHealthState(healthy=False),
                bootstrap=PaperclipBootstrapState(ready=False),
                config=config,
                notes=(
                    f"Paperclip service '{service_name}' will be exposed at '{hostname}'.",
                    f"Paperclip requires authenticated-private access on '{hostname}'.",
                    f"Paperclip health is verified via '{health_url}'.",
                    f"Paperclip persists PAPERCLIP_HOME in '{data_name}'.",
                ),
            ),
            service_resource_id=None,
            data_resource_id=None,
        )

    initial_health = backend.check_health(service=service, url=health_url)
    bootstrap = backend.ensure_application_ready(
        better_auth_secret_ref=better_auth_secret_ref,
    )
    final_health = initial_health or backend.check_health(service=service, url=health_url)
    if not final_health:
        raise PaperclipError(f"Paperclip health check failed for '{health_url}'.")

    return PaperclipPhase(
        result=PaperclipResult(
            outcome=(
                "applied"
                if {service_action, data_action} & {"create", "update_owned"}
                else "already_present"
            ),
            enabled=True,
            hostname=hostname,
            service=service,
            health=PaperclipHealthState(healthy=True),
            bootstrap=bootstrap,
            config=config,
            notes=(
                f"Paperclip service '{service_name}' is reconciled and healthy.",
                f"Paperclip persists PAPERCLIP_HOME in '{data_name}'.",
                "Paperclip remains locked to authenticated-private browser access.",
            ),
        ),
        service_resource_id=service_id,
        data_resource_id=data_id,
    )


def build_paperclip_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    service_resource_id: str | None,
    data_resource_id: str | None,
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if not (
            (
                resource.resource_type == PAPERCLIP_SERVICE_RESOURCE_TYPE
                and resource.scope == _service_scope(stack_name)
            )
            or (
                resource.resource_type == PAPERCLIP_DATA_RESOURCE_TYPE
                and resource.scope == _data_scope(stack_name)
            )
        )
    ]
    if service_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=PAPERCLIP_SERVICE_RESOURCE_TYPE,
                resource_id=service_resource_id,
                scope=_service_scope(stack_name),
            )
        )
    if data_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=PAPERCLIP_DATA_RESOURCE_TYPE,
                resource_id=data_resource_id,
                scope=_data_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _get_paperclip_allocation(
    desired_state: DesiredState,
) -> tuple[PackSharedAllocation, str, SharedPostgresAllocation]:
    matches = [
        allocation
        for allocation in desired_state.shared_core.allocations
        if allocation.pack_name == "paperclip"
    ]
    if (
        len(matches) != 1
        or matches[0].postgres is None
        or desired_state.shared_core.postgres is None
    ):
        raise PaperclipError(
            "Paperclip shared-core postgres allocation is missing from desired "
            "state."
        )
    return matches[0], desired_state.shared_core.postgres.service_name, matches[0].postgres


def _build_env_contract(
    *,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    paperclip_home: str,
    better_auth_secret_ref: str,
    public_url: str,
    openclaw_gateway_url: str | None = None,
    openclaw_gateway_token_secret_ref: str | None = None,
    workspace_daemon_base_url: str | None = None,
    workspace_daemon_token: str | None = None,
) -> dict[str, str]:
    env = {
        "DATABASE_URL": (
            f"postgres://{postgres.user_name}:{_secret_placeholder(postgres.password_secret_ref)}"
            f"@{postgres_service_name}:5432/{postgres.database_name}?sslmode=disable"
        ),
        "PAPERCLIP_HOME": paperclip_home,
        "BETTER_AUTH_SECRET": _secret_placeholder(better_auth_secret_ref),
        "PAPERCLIP_PUBLIC_URL": public_url,
    }
    if (openclaw_gateway_url is None) != (openclaw_gateway_token_secret_ref is None):
        raise PaperclipError(
            "Paperclip OpenClaw adapter requires both OPENCLAW_GATEWAY_URL "
            "and an OpenClaw gateway token secret reference."
        )
    if openclaw_gateway_url is not None and openclaw_gateway_token_secret_ref is not None:
        env["OPENCLAW_GATEWAY_URL"] = openclaw_gateway_url
        env["OPENCLAW_GATEWAY_TOKEN"] = _secret_placeholder(openclaw_gateway_token_secret_ref)

    if (workspace_daemon_base_url is None) != (workspace_daemon_token is None):
        raise PaperclipError(
            "Paperclip workspace daemon adapter requires both "
            "WORKSPACE_DAEMON_BASE_URL and WORKSPACE_DAEMON_TOKEN."
        )
    if workspace_daemon_base_url is not None and workspace_daemon_token is not None:
        env["WORKSPACE_DAEMON_BASE_URL"] = workspace_daemon_base_url
        env["WORKSPACE_DAEMON_TOKEN"] = workspace_daemon_token
    return env


def _secret_placeholder(secret_ref: str) -> str:
    return f"secretref:{secret_ref}"


def _resolve_owned_resource(
    *,
    dry_run: bool,
    resource_name: str,
    resource_type: str,
    owned_resource: OwnedResource | None,
    get_resource: Callable[[str], PaperclipResourceRecord | None],
    find_by_name: Callable[[str], PaperclipResourceRecord | None],
    create_resource: Callable[[str], PaperclipResourceRecord],
) -> tuple[PaperclipResourceRecord, str, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise PaperclipError(
                f"Ownership ledger says the Paperclip {resource_type} exists, "
                "but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise PaperclipError(
                f"Ownership ledger Paperclip {resource_type} no longer "
                "matches the desired naming convention."
            )
        return existing, existing.resource_id, "reuse_owned"

    existing = find_by_name(resource_name)
    if existing is not None:
        if dry_run:
            return existing, existing.resource_id, "reuse_existing"
        created = create_resource(resource_name)
        return created, created.resource_id, "reuse_existing"

    if dry_run:
        planned_id = f"planned-{resource_type}:{resource_name}"
        return (
            PaperclipResourceRecord(resource_id=planned_id, resource_name=resource_name),
            planned_id,
            "create",
        )

    created = create_resource(resource_name)
    return created, created.resource_id, "create"


def _resolve_service(
    *,
    dry_run: bool,
    service_name: str,
    hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    data_name: str,
    env: dict[str, str],
    ownership_ledger: OwnershipLedger,
    stack_name: str,
    backend: PaperclipBackend,
) -> tuple[PaperclipResourceRecord, str, str]:
    owned_resource = _find_owned_resource(
        ownership_ledger=ownership_ledger,
        resource_type=PAPERCLIP_SERVICE_RESOURCE_TYPE,
        scope=_service_scope(stack_name),
    )
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise PaperclipError(
                "Ownership ledger says the Paperclip service exists, but the "
                "backend could not find it."
            )
        if existing.resource_name != service_name:
            raise PaperclipError(
                "Ownership ledger Paperclip service no longer matches the "
                "desired naming convention."
            )
        if dry_run:
            return existing, existing.resource_id, "reuse_owned"
        updated = backend.update_service(
            resource_id=existing.resource_id,
            resource_name=service_name,
            hostname=hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
            env=env,
        )
        return updated, updated.resource_id, "update_owned"

    existing = backend.find_service_by_name(service_name)
    if existing is not None:
        if dry_run:
            return existing, existing.resource_id, "reuse_existing"
        created = backend.create_service(
            resource_name=service_name,
            hostname=hostname,
            postgres_service_name=postgres_service_name,
            postgres=postgres,
            data_resource_name=data_name,
            env=env,
        )
        return created, created.resource_id, "reuse_existing"

    if dry_run:
        planned_id = f"planned-service:{service_name}"
        return (
            PaperclipResourceRecord(resource_id=planned_id, resource_name=service_name),
            planned_id,
            "create",
        )

    created = backend.create_service(
        resource_name=service_name,
        hostname=hostname,
        postgres_service_name=postgres_service_name,
        postgres=postgres,
        data_resource_name=data_name,
        env=env,
    )
    return created, created.resource_id, "create"


def _find_owned_resource(
    *, ownership_ledger: OwnershipLedger, resource_type: str, scope: str
) -> OwnedResource | None:
    return next(
        (
            resource
            for resource in ownership_ledger.resources
            if resource.resource_type == resource_type and resource.scope == scope
        ),
        None,
    )


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-paperclip"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-paperclip-home"


def _better_auth_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-paperclip-better-auth-secret"


def _openclaw_gateway_token_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-openclaw-gateway-token"


def _openclaw_gateway_url(desired_state: DesiredState) -> str:
    hostname = (
        desired_state.machine_hostnames.get("openclaw-internal")
        or desired_state.hostnames.get("openclaw-internal")
    )
    if hostname is None:
        raise PaperclipError(
            "Desired state is missing the OpenClaw internal hostname at "
            "hostnames['openclaw-internal']."
        )
    return f"https://{hostname}"


def _service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:paperclip:service"


def _data_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:paperclip:data"


def _http_health_check(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    connection = http.client.HTTPSConnection("127.0.0.1", 443, timeout=10)
    try:
        connection.request(
            "GET",
            parsed.path or "/",
            headers={"Host": parsed.netloc},
        )
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


def _optional_env(key: str) -> str | None:
    value = os.getenv(key)
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized
