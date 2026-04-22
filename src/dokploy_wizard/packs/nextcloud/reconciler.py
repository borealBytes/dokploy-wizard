# pyright: reportMissingImports=false

"""Nextcloud + OnlyOffice runtime reconciliation and ledger integration."""

from __future__ import annotations

import http.client
from typing import Protocol

from dokploy_wizard.core.models import (
    PackSharedAllocation,
    SharedPostgresAllocation,
    SharedRedisAllocation,
)
from dokploy_wizard.packs.nextcloud.models import (
    NextcloudCommandCheck,
    NextcloudHealthCheck,
    NextcloudManagedResource,
    NextcloudPhase,
    NextcloudPostgresBinding,
    NextcloudRedisBinding,
    NextcloudBundleVerification,
    NextcloudResourceRecord,
    NextcloudResult,
    NextcloudServiceConfig,
    NextcloudServiceRuntime,
    OnlyofficeServiceConfig,
    OnlyofficeServiceRuntime,
    TalkRuntime,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput

NEXTCLOUD_SERVICE_RESOURCE_TYPE = "nextcloud_service"
ONLYOFFICE_SERVICE_RESOURCE_TYPE = "onlyoffice_service"
NEXTCLOUD_VOLUME_RESOURCE_TYPE = "nextcloud_volume"
ONLYOFFICE_VOLUME_RESOURCE_TYPE = "onlyoffice_volume"


class NextcloudError(RuntimeError):
    """Raised when Nextcloud reconciliation fails or detects drift."""


class NextcloudBackend(Protocol):
    def get_service(self, resource_id: str) -> NextcloudResourceRecord | None: ...

    def find_service_by_name(self, resource_name: str) -> NextcloudResourceRecord | None: ...

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord: ...

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord: ...

    def get_volume(self, resource_id: str) -> NextcloudResourceRecord | None: ...

    def find_volume_by_name(self, resource_name: str) -> NextcloudResourceRecord | None: ...

    def create_volume(self, *, resource_name: str) -> NextcloudResourceRecord: ...

    def ensure_application_ready(
        self, *, nextcloud_url: str, onlyoffice_url: str
    ) -> NextcloudBundleVerification: ...

    def refresh_openclaw_external_storage(self, *, admin_user: str) -> None: ...

    def check_health(self, *, service: NextcloudResourceRecord, url: str) -> bool: ...


class ShellNextcloudBackend:
    """Deterministic default backend for Nextcloud planning and health checks."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        stack_name = values.get("STACK_NAME", "")
        self._forced_service_ids = {
            _nextcloud_service_name(stack_name): values.get(
                "NEXTCLOUD_MOCK_EXISTING_NEXTCLOUD_SERVICE_ID"
            ),
            _onlyoffice_service_name(stack_name): values.get(
                "NEXTCLOUD_MOCK_EXISTING_ONLYOFFICE_SERVICE_ID"
            ),
        }
        self._forced_volume_ids = {
            _nextcloud_volume_name(stack_name): values.get(
                "NEXTCLOUD_MOCK_EXISTING_NEXTCLOUD_VOLUME_ID"
            ),
            _onlyoffice_volume_name(stack_name): values.get(
                "NEXTCLOUD_MOCK_EXISTING_ONLYOFFICE_VOLUME_ID"
            ),
        }
        self._forced_health = {
            _nextcloud_service_name(stack_name): _optional_bool(
                values, "NEXTCLOUD_MOCK_NEXTCLOUD_HEALTHY"
            ),
            _onlyoffice_service_name(stack_name): _optional_bool(
                values, "NEXTCLOUD_MOCK_ONLYOFFICE_HEALTHY"
            ),
        }
        self._services: dict[str, NextcloudResourceRecord] = {}
        self._volumes: dict[str, NextcloudResourceRecord] = {}

    def get_service(self, resource_id: str) -> NextcloudResourceRecord | None:
        for record in self._services.values():
            if record.resource_id == resource_id:
                return record
        for resource_name, forced_id in self._forced_service_ids.items():
            if forced_id == resource_id:
                return NextcloudResourceRecord(resource_id=resource_id, resource_name=resource_name)
        return NextcloudResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_service_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        existing = self._services.get(resource_name)
        if existing is not None:
            return existing
        forced_id = self._forced_service_ids.get(resource_name)
        if forced_id is None:
            return None
        return NextcloudResourceRecord(resource_id=forced_id, resource_name=resource_name)

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        del hostname, data_volume_name, config
        record = NextcloudResourceRecord(resource_id=resource_name, resource_name=resource_name)
        self._services[resource_name] = record
        return record

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        data_volume_name: str,
        config: dict[str, str],
    ) -> NextcloudResourceRecord:
        del resource_id
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            data_volume_name=data_volume_name,
            config=config,
        )

    def get_volume(self, resource_id: str) -> NextcloudResourceRecord | None:
        for record in self._volumes.values():
            if record.resource_id == resource_id:
                return record
        for resource_name, forced_id in self._forced_volume_ids.items():
            if forced_id == resource_id:
                return NextcloudResourceRecord(resource_id=resource_id, resource_name=resource_name)
        return NextcloudResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_volume_by_name(self, resource_name: str) -> NextcloudResourceRecord | None:
        existing = self._volumes.get(resource_name)
        if existing is not None:
            return existing
        forced_id = self._forced_volume_ids.get(resource_name)
        if forced_id is None:
            return None
        return NextcloudResourceRecord(resource_id=forced_id, resource_name=resource_name)

    def create_volume(self, *, resource_name: str) -> NextcloudResourceRecord:
        record = NextcloudResourceRecord(resource_id=resource_name, resource_name=resource_name)
        self._volumes[resource_name] = record
        return record

    def check_health(self, *, service: NextcloudResourceRecord, url: str) -> bool:
        forced = self._forced_health.get(service.resource_name)
        if forced is not None:
            return forced
        return _http_health_check(url)

    def ensure_application_ready(
        self, *, nextcloud_url: str, onlyoffice_url: str
    ) -> NextcloudBundleVerification:
        del nextcloud_url, onlyoffice_url
        return NextcloudBundleVerification(
            onlyoffice_document_server_check=NextcloudCommandCheck(
                command="php occ onlyoffice:documentserver --check",
                passed=True,
            ),
            talk=TalkRuntime(
                app_id="spreed",
                enabled=True,
                enabled_check=NextcloudCommandCheck(
                    command="php occ app:list --output=json",
                    passed=True,
                ),
                signaling_check=NextcloudCommandCheck(
                    command="php occ talk:signaling:list --output=json",
                    passed=True,
                ),
                stun_check=NextcloudCommandCheck(
                    command="php occ talk:stun:list --output=json",
                    passed=True,
                ),
                turn_check=NextcloudCommandCheck(
                    command="php occ talk:turn:list --output=json",
                    passed=True,
                ),
            ),
        )

    def refresh_openclaw_external_storage(self, *, admin_user: str) -> None:
        del admin_user


def reconcile_nextcloud(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: NextcloudBackend,
) -> NextcloudPhase:
    if "nextcloud" not in desired_state.enabled_packs:
        return NextcloudPhase(
            result=NextcloudResult(
                outcome="skipped",
                enabled=False,
                nextcloud=None,
                onlyoffice=None,
                talk=None,
                notes=("Nextcloud + OnlyOffice pack is explicitly disabled for this install.",),
            ),
            nextcloud_service_resource_id=None,
            onlyoffice_service_resource_id=None,
            nextcloud_volume_resource_id=None,
            onlyoffice_volume_resource_id=None,
        )

    nextcloud_hostname = desired_state.hostnames.get("nextcloud")
    if nextcloud_hostname is None:
        raise NextcloudError(
            "Desired state is missing the canonical Nextcloud hostname at hostnames['nextcloud']."
        )
    onlyoffice_hostname = desired_state.hostnames.get("onlyoffice")
    if onlyoffice_hostname is None:
        raise NextcloudError(
            "Desired state is missing the canonical OnlyOffice hostname at hostnames['onlyoffice']."
        )

    allocation = _get_nextcloud_allocation(desired_state)
    postgres = _require_postgres(allocation)
    redis = _require_redis(allocation)
    nextcloud_service_name = _nextcloud_service_name(desired_state.stack_name)
    onlyoffice_service_name = _onlyoffice_service_name(desired_state.stack_name)
    nextcloud_volume_name = _nextcloud_volume_name(desired_state.stack_name)
    onlyoffice_volume_name = _onlyoffice_volume_name(desired_state.stack_name)
    nextcloud_url = f"https://{nextcloud_hostname}"
    onlyoffice_url = f"https://{onlyoffice_hostname}"
    integration_secret_ref = _integration_secret_ref(desired_state.stack_name)

    nextcloud_volume, nextcloud_volume_id = _resolve_volume(
        dry_run=dry_run,
        ownership_ledger=ownership_ledger,
        resource_name=nextcloud_volume_name,
        resource_type=NEXTCLOUD_VOLUME_RESOURCE_TYPE,
        scope=_nextcloud_volume_scope(desired_state.stack_name),
        backend=backend,
    )
    onlyoffice_volume, onlyoffice_volume_id = _resolve_volume(
        dry_run=dry_run,
        ownership_ledger=ownership_ledger,
        resource_name=onlyoffice_volume_name,
        resource_type=ONLYOFFICE_VOLUME_RESOURCE_TYPE,
        scope=_onlyoffice_volume_scope(desired_state.stack_name),
        backend=backend,
    )
    nextcloud_service, nextcloud_service_id = _resolve_service(
        dry_run=dry_run,
        ownership_ledger=ownership_ledger,
        resource_name=nextcloud_service_name,
        hostname=nextcloud_hostname,
        data_volume_name=nextcloud_volume_name,
        config={
            "onlyoffice_url": onlyoffice_url,
            "postgres_database_name": postgres.database_name,
            "postgres_password_secret_ref": postgres.password_secret_ref,
            "postgres_user_name": postgres.user_name,
            "redis_identity_name": redis.identity_name,
            "redis_password_secret_ref": redis.password_secret_ref,
        },
        resource_type=NEXTCLOUD_SERVICE_RESOURCE_TYPE,
        scope=_nextcloud_service_scope(desired_state.stack_name),
        backend=backend,
    )
    onlyoffice_service, onlyoffice_service_id = _resolve_service(
        dry_run=dry_run,
        ownership_ledger=ownership_ledger,
        resource_name=onlyoffice_service_name,
        hostname=onlyoffice_hostname,
        data_volume_name=onlyoffice_volume_name,
        config={
            "integration_secret_ref": integration_secret_ref,
            "nextcloud_url": nextcloud_url,
        },
        resource_type=ONLYOFFICE_SERVICE_RESOURCE_TYPE,
        scope=_onlyoffice_service_scope(desired_state.stack_name),
        backend=backend,
    )

    nextcloud_runtime = NextcloudServiceRuntime(
        hostname=nextcloud_hostname,
        url=nextcloud_url,
        service=nextcloud_service,
        data_volume=nextcloud_volume,
        health_check=NextcloudHealthCheck(url=f"{nextcloud_url}/status.php", passed=None),
        config=NextcloudServiceConfig(
            onlyoffice_url=onlyoffice_url,
            postgres=NextcloudPostgresBinding(
                database_name=postgres.database_name,
                user_name=postgres.user_name,
                password_secret_ref=postgres.password_secret_ref,
            ),
            redis=NextcloudRedisBinding(
                identity_name=redis.identity_name,
                password_secret_ref=redis.password_secret_ref,
            ),
        ),
    )
    onlyoffice_runtime = OnlyofficeServiceRuntime(
        hostname=onlyoffice_hostname,
        url=onlyoffice_url,
        service=onlyoffice_service,
        data_volume=onlyoffice_volume,
        health_check=NextcloudHealthCheck(url=f"{onlyoffice_url}/healthcheck", passed=None),
        config=OnlyofficeServiceConfig(
            nextcloud_url=nextcloud_url,
            integration_secret_ref=integration_secret_ref,
        ),
    )

    if dry_run:
        return NextcloudPhase(
            result=NextcloudResult(
                outcome="plan_only",
                enabled=True,
                nextcloud=nextcloud_runtime,
                onlyoffice=onlyoffice_runtime,
                talk=None,
                notes=(
                    "Nextcloud reuses the shared-core postgres and redis allocation "
                    "for pack_name='nextcloud'.",
                    "OnlyOffice is deployed only as the paired office runtime for Nextcloud in v1.",
                    "Non-dry-run success is gated on both Nextcloud and OnlyOffice health checks.",
                ),
            ),
            nextcloud_service_resource_id=None,
            onlyoffice_service_resource_id=None,
            nextcloud_volume_resource_id=None,
            onlyoffice_volume_resource_id=None,
        )

    bundle = backend.ensure_application_ready(
        nextcloud_url=nextcloud_url,
        onlyoffice_url=onlyoffice_url,
    )

    nextcloud_ok = backend.check_health(
        service=NextcloudResourceRecord(
            resource_id=nextcloud_service_id,
            resource_name=nextcloud_service.resource_name,
        ),
        url=f"{nextcloud_url}/status.php",
    )
    if not nextcloud_ok:
        raise NextcloudError(
            "Nextcloud application bootstrap did not finish cleanly for "
            f"'{nextcloud_url}/status.php'."
        )
    onlyoffice_ok = backend.check_health(
        service=NextcloudResourceRecord(
            resource_id=onlyoffice_service_id,
            resource_name=onlyoffice_service.resource_name,
        ),
        url=f"{onlyoffice_url}/healthcheck",
    )
    if not onlyoffice_ok:
        raise NextcloudError(f"OnlyOffice health check failed for '{onlyoffice_url}/healthcheck'.")

    actions = {
        nextcloud_service.action,
        onlyoffice_service.action,
        nextcloud_volume.action,
        onlyoffice_volume.action,
    }
    nextcloud_runtime = NextcloudServiceRuntime(
        hostname=nextcloud_runtime.hostname,
        url=nextcloud_runtime.url,
        service=nextcloud_runtime.service,
        data_volume=nextcloud_runtime.data_volume,
        health_check=NextcloudHealthCheck(url=nextcloud_runtime.health_check.url, passed=True),
        config=nextcloud_runtime.config,
    )
    onlyoffice_runtime = OnlyofficeServiceRuntime(
        hostname=onlyoffice_runtime.hostname,
        url=onlyoffice_runtime.url,
        service=onlyoffice_runtime.service,
        data_volume=onlyoffice_runtime.data_volume,
        health_check=NextcloudHealthCheck(url=onlyoffice_runtime.health_check.url, passed=True),
        config=onlyoffice_runtime.config,
    )
    return NextcloudPhase(
        result=NextcloudResult(
            outcome="applied" if "create" in actions else "already_present",
            enabled=True,
            nextcloud=nextcloud_runtime,
            onlyoffice=onlyoffice_runtime,
            talk=bundle.talk,
            notes=(
                "Nextcloud, OnlyOffice, and Talk are reconciled together and reported healthy.",
                "Secret refs are deterministic names only; secret values are not persisted.",
            ),
        ),
        nextcloud_service_resource_id=nextcloud_service_id,
        onlyoffice_service_resource_id=onlyoffice_service_id,
        nextcloud_volume_resource_id=nextcloud_volume_id,
        onlyoffice_volume_resource_id=onlyoffice_volume_id,
    )


def build_nextcloud_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    nextcloud_service_resource_id: str | None,
    onlyoffice_service_resource_id: str | None,
    nextcloud_volume_resource_id: str | None,
    onlyoffice_volume_resource_id: str | None,
) -> OwnershipLedger:
    managed_scopes = {
        _nextcloud_service_scope(stack_name),
        _onlyoffice_service_scope(stack_name),
        _nextcloud_volume_scope(stack_name),
        _onlyoffice_volume_scope(stack_name),
    }
    resources = [
        resource for resource in existing_ledger.resources if resource.scope not in managed_scopes
    ]
    additions = (
        (
            NEXTCLOUD_SERVICE_RESOURCE_TYPE,
            nextcloud_service_resource_id,
            _nextcloud_service_scope(stack_name),
        ),
        (
            ONLYOFFICE_SERVICE_RESOURCE_TYPE,
            onlyoffice_service_resource_id,
            _onlyoffice_service_scope(stack_name),
        ),
        (
            NEXTCLOUD_VOLUME_RESOURCE_TYPE,
            nextcloud_volume_resource_id,
            _nextcloud_volume_scope(stack_name),
        ),
        (
            ONLYOFFICE_VOLUME_RESOURCE_TYPE,
            onlyoffice_volume_resource_id,
            _onlyoffice_volume_scope(stack_name),
        ),
    )
    for resource_type, resource_id, scope in additions:
        if resource_id is None:
            continue
        resources.append(
            OwnedResource(resource_type=resource_type, resource_id=resource_id, scope=scope)
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _get_nextcloud_allocation(desired_state: DesiredState) -> PackSharedAllocation:
    matches = [
        allocation
        for allocation in desired_state.shared_core.allocations
        if allocation.pack_name == "nextcloud"
    ]
    if not matches:
        raise NextcloudError(
            "Nextcloud is enabled but the required shared-core allocation for "
            "pack_name 'nextcloud' is missing."
        )
    if len(matches) > 1:
        raise NextcloudError(
            "Shared-core allocations contain multiple entries for pack_name 'nextcloud'."
        )
    return matches[0]


def _require_postgres(allocation: PackSharedAllocation) -> SharedPostgresAllocation:
    if allocation.postgres is None:
        raise NextcloudError(
            "Nextcloud requires a shared-core postgres allocation, but none was "
            "planned for pack_name 'nextcloud'."
        )
    return allocation.postgres


def _require_redis(allocation: PackSharedAllocation) -> SharedRedisAllocation:
    if allocation.redis is None:
        raise NextcloudError(
            "Nextcloud requires a shared-core redis allocation, but none was "
            "planned for pack_name 'nextcloud'."
        )
    return allocation.redis


def _resolve_service(
    *,
    dry_run: bool,
    ownership_ledger: OwnershipLedger,
    resource_name: str,
    hostname: str,
    data_volume_name: str,
    config: dict[str, str],
    resource_type: str,
    scope: str,
    backend: NextcloudBackend,
) -> tuple[NextcloudManagedResource, str]:
    owned_resource = _find_owned_resource(ownership_ledger, resource_type, scope)
    if owned_resource is not None:
        existing = backend.get_service(owned_resource.resource_id)
        if existing is None:
            raise NextcloudError(
                "Ownership ledger says runtime resource "
                f"'{resource_type}' exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise NextcloudError(
                "Ownership ledger resource "
                f"'{resource_type}' no longer matches the desired naming convention."
            )
        if not dry_run:
            updated = backend.update_service(
                resource_id=existing.resource_id,
                resource_name=resource_name,
                hostname=hostname,
                data_volume_name=data_volume_name,
                config=config,
            )
            return (
                NextcloudManagedResource(
                    action="update_owned",
                    resource_id=updated.resource_id,
                    resource_name=updated.resource_name,
                ),
                updated.resource_id,
            )
        return (
            NextcloudManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    collision = backend.find_service_by_name(resource_name)
    if collision is not None:
        if collision.resource_id.startswith("dokploy-compose:"):
            if not dry_run:
                updated = backend.update_service(
                    resource_id=collision.resource_id,
                    resource_name=resource_name,
                    hostname=hostname,
                    data_volume_name=data_volume_name,
                    config=config,
                )
                return (
                    NextcloudManagedResource(
                        action="reuse_existing",
                        resource_id=updated.resource_id,
                        resource_name=updated.resource_name,
                    ),
                    updated.resource_id,
                )
            return (
                NextcloudManagedResource(
                    action="reuse_existing",
                    resource_id=collision.resource_id,
                    resource_name=collision.resource_name,
                ),
                collision.resource_id,
            )
        raise NextcloudError(
            f"Refusing to adopt existing unowned service '{resource_name}' for '{resource_type}'."
        )

    if dry_run:
        planned_id = f"planned:{resource_name}"
        return (
            NextcloudManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = backend.create_service(
        resource_name=resource_name,
        hostname=hostname,
        data_volume_name=data_volume_name,
        config=config,
    )
    return (
        NextcloudManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _resolve_volume(
    *,
    dry_run: bool,
    ownership_ledger: OwnershipLedger,
    resource_name: str,
    resource_type: str,
    scope: str,
    backend: NextcloudBackend,
) -> tuple[NextcloudManagedResource, str]:
    owned_resource = _find_owned_resource(ownership_ledger, resource_type, scope)
    if owned_resource is not None:
        existing = backend.get_volume(owned_resource.resource_id)
        if existing is None:
            raise NextcloudError(
                "Ownership ledger says runtime resource "
                f"'{resource_type}' exists, but the backend could not find it."
            )
        if existing.resource_name != resource_name:
            raise NextcloudError(
                "Ownership ledger resource "
                f"'{resource_type}' no longer matches the desired naming convention."
            )
        return (
            NextcloudManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    collision = backend.find_volume_by_name(resource_name)
    if collision is not None:
        if collision.resource_id.startswith("dokploy-compose:"):
            return (
                NextcloudManagedResource(
                    action="reuse_existing",
                    resource_id=collision.resource_id,
                    resource_name=collision.resource_name,
                ),
                collision.resource_id,
            )
        raise NextcloudError(
            f"Refusing to adopt existing unowned volume '{resource_name}' for '{resource_type}'."
        )

    if dry_run:
        planned_id = f"planned:{resource_name}"
        return (
            NextcloudManagedResource(
                action="create",
                resource_id=planned_id,
                resource_name=resource_name,
            ),
            planned_id,
        )

    created = backend.create_volume(resource_name=resource_name)
    return (
        NextcloudManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _find_owned_resource(
    ownership_ledger: OwnershipLedger, resource_type: str, scope: str
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == resource_type and resource.scope == scope
    ]
    if len(matches) > 1:
        raise NextcloudError(
            f"Ownership ledger contains multiple '{resource_type}' resources for scope '{scope}'."
        )
    return matches[0] if matches else None


def _nextcloud_service_name(stack_name: str) -> str:
    return f"{stack_name}-nextcloud"


def _onlyoffice_service_name(stack_name: str) -> str:
    return f"{stack_name}-onlyoffice"


def _nextcloud_volume_name(stack_name: str) -> str:
    return f"{stack_name}-nextcloud-data"


def _onlyoffice_volume_name(stack_name: str) -> str:
    return f"{stack_name}-onlyoffice-data"


def _integration_secret_ref(stack_name: str) -> str:
    return f"{stack_name}-nextcloud-onlyoffice-jwt-secret"


def _nextcloud_service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:nextcloud-service"


def _onlyoffice_service_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:onlyoffice-service"


def _nextcloud_volume_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:nextcloud-volume"


def _onlyoffice_volume_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:onlyoffice-volume"


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise NextcloudError(f"Invalid boolean value for '{key}': {raw_value!r}.")


def _http_health_check(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    host, path = url.removeprefix("https://").split("/", 1)
    connection: http.client.HTTPSConnection | None = None
    try:
        connection = http.client.HTTPSConnection(host, timeout=2.0)
        connection.request("GET", f"/{path}")
        response = connection.getresponse()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()
