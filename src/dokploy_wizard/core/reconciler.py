"""Shared-core reconciliation and ledger integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from dokploy_wizard.core.models import (
    SharedCoreManagedResource,
    SharedCorePhase,
    SharedCorePlan,
    SharedCoreResourceRecord,
    SharedCoreResult,
    SharedPostgresAllocation,
)
from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger

SHARED_NETWORK_RESOURCE_TYPE = "shared_core_network"
SHARED_POSTGRES_RESOURCE_TYPE = "shared_core_postgres"
SHARED_REDIS_RESOURCE_TYPE = "shared_core_redis"
SHARED_MAIL_RELAY_RESOURCE_TYPE = "shared_core_mail_relay"


class SharedCoreError(RuntimeError):
    """Raised when shared-core reconciliation fails or detects drift."""


class SharedCoreBackend(Protocol):
    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None: ...

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None: ...

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord: ...

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None: ...

    def find_postgres_service_by_name(
        self, resource_name: str
    ) -> SharedCoreResourceRecord | None: ...

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord: ...

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None: ...

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None: ...

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord: ...

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None: ...

    def find_mail_relay_service_by_name(
        self, resource_name: str
    ) -> SharedCoreResourceRecord | None: ...

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord: ...


class ShellSharedCoreBackend:
    """Deterministic default backend for shared-core naming conventions.

    Task 5 only establishes wizard-owned shared-core conventions. Later pack tasks can
    swap this backend for real platform provisioning without changing the planner or
    ledger contracts.
    """

    def get_network(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return SharedCoreResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_network_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return None

    def create_network(self, resource_name: str) -> SharedCoreResourceRecord:
        return SharedCoreResourceRecord(resource_id=resource_name, resource_name=resource_name)

    def get_postgres_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return SharedCoreResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_postgres_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return None

    def create_postgres_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return SharedCoreResourceRecord(resource_id=resource_name, resource_name=resource_name)

    def get_redis_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return SharedCoreResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_redis_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return None

    def create_redis_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return SharedCoreResourceRecord(resource_id=resource_name, resource_name=resource_name)

    def get_mail_relay_service(self, resource_id: str) -> SharedCoreResourceRecord | None:
        return SharedCoreResourceRecord(resource_id=resource_id, resource_name=resource_id)

    def find_mail_relay_service_by_name(self, resource_name: str) -> SharedCoreResourceRecord | None:
        return None

    def create_mail_relay_service(self, resource_name: str) -> SharedCoreResourceRecord:
        return SharedCoreResourceRecord(resource_id=resource_name, resource_name=resource_name)

    def ensure_postgres_allocations(
        self, allocations: tuple[SharedPostgresAllocation, ...]
    ) -> None:
        del allocations


def reconcile_shared_core(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: SharedCoreBackend,
) -> SharedCorePhase:
    plan = desired_state.shared_core
    if not plan.requires_reconciliation():
        return SharedCorePhase(
            result=SharedCoreResult(
                outcome="not_required",
                network=None,
                postgres=None,
                redis=None,
                mail_relay=None,
                allocations=plan.allocations,
                notes=("No selected packs require shared PostgreSQL, Redis, or SMTP relay services.",),
            ),
            network_resource_id=None,
            postgres_resource_id=None,
            redis_resource_id=None,
            mail_relay_resource_id=None,
        )

    network, network_id = _resolve_network(
        dry_run=dry_run,
        stack_name=desired_state.stack_name,
        plan=plan,
        ownership_ledger=ownership_ledger,
        backend=backend,
    )
    postgres, postgres_id = _resolve_optional_service(
        dry_run=dry_run,
        ownership_ledger=ownership_ledger,
        service_name=None if plan.postgres is None else plan.postgres.service_name,
        resource_type=SHARED_POSTGRES_RESOURCE_TYPE,
        scope=_postgres_scope(desired_state.stack_name),
        get_resource=backend.get_postgres_service,
        find_by_name=backend.find_postgres_service_by_name,
        create_resource=backend.create_postgres_service,
    )
    redis, redis_id = _resolve_optional_service(
        dry_run=dry_run,
        ownership_ledger=ownership_ledger,
        service_name=None if plan.redis is None else plan.redis.service_name,
        resource_type=SHARED_REDIS_RESOURCE_TYPE,
        scope=_redis_scope(desired_state.stack_name),
        get_resource=backend.get_redis_service,
        find_by_name=backend.find_redis_service_by_name,
        create_resource=backend.create_redis_service,
    )
    if plan.mail_relay is None:
        mail_relay, mail_relay_id = None, None
    else:
        mail_relay, mail_relay_id = _resolve_optional_service(
            dry_run=dry_run,
            ownership_ledger=ownership_ledger,
            service_name=plan.mail_relay.service_name,
            resource_type=SHARED_MAIL_RELAY_RESOURCE_TYPE,
            scope=_mail_relay_scope(desired_state.stack_name),
            get_resource=backend.get_mail_relay_service,
            find_by_name=backend.find_mail_relay_service_by_name,
            create_resource=backend.create_mail_relay_service,
        )
    if not dry_run and postgres is not None:
        ensure_postgres_allocations = getattr(backend, "ensure_postgres_allocations", None)
        if callable(ensure_postgres_allocations):
            ensure_postgres_allocations(
                tuple(
                    allocation.postgres
                    for allocation in plan.allocations
                    if allocation.postgres is not None
                )
            )
    actions = [network.action]
    if postgres is not None:
        actions.append(postgres.action)
    if redis is not None:
        actions.append(redis.action)
    if mail_relay is not None:
        actions.append(mail_relay.action)

    return SharedCorePhase(
        result=SharedCoreResult(
            outcome="plan_only" if dry_run else _derive_outcome(tuple(actions)),
            network=network,
            postgres=postgres,
            redis=redis,
            mail_relay=mail_relay,
            allocations=plan.allocations,
            notes=(
                f"Shared network '{plan.network_name}' planned for shared-core packs.",
                f"Per-pack allocations prepared for {len(plan.allocations)} selected pack(s).",
            ),
        ),
        network_resource_id=None if dry_run else network_id,
        postgres_resource_id=None if dry_run else postgres_id,
        redis_resource_id=None if dry_run else redis_id,
        mail_relay_resource_id=None if dry_run else mail_relay_id,
    )


def build_shared_core_ledger(
    *,
    existing_ledger: OwnershipLedger,
    stack_name: str,
    network_resource_id: str | None,
    postgres_resource_id: str | None,
    redis_resource_id: str | None,
    mail_relay_resource_id: str | None,
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if resource.resource_type
        not in {
            SHARED_NETWORK_RESOURCE_TYPE,
            SHARED_POSTGRES_RESOURCE_TYPE,
            SHARED_REDIS_RESOURCE_TYPE,
            SHARED_MAIL_RELAY_RESOURCE_TYPE,
        }
    ]
    if network_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SHARED_NETWORK_RESOURCE_TYPE,
                resource_id=network_resource_id,
                scope=_network_scope(stack_name),
            )
        )
    if postgres_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SHARED_POSTGRES_RESOURCE_TYPE,
                resource_id=postgres_resource_id,
                scope=_postgres_scope(stack_name),
            )
        )
    if redis_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SHARED_REDIS_RESOURCE_TYPE,
                resource_id=redis_resource_id,
                scope=_redis_scope(stack_name),
            )
        )
    if mail_relay_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=SHARED_MAIL_RELAY_RESOURCE_TYPE,
                resource_id=mail_relay_resource_id,
                scope=_mail_relay_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version,
        resources=tuple(resources),
    )


def _resolve_network(
    *,
    dry_run: bool,
    stack_name: str,
    plan: SharedCorePlan,
    ownership_ledger: OwnershipLedger,
    backend: SharedCoreBackend,
) -> tuple[SharedCoreManagedResource, str]:
    return _resolve_resource(
        dry_run=dry_run,
        service_name=plan.network_name,
        owned_resource=_find_owned_resource(
            ownership_ledger,
            SHARED_NETWORK_RESOURCE_TYPE,
            _network_scope(stack_name),
        ),
        resource_type=SHARED_NETWORK_RESOURCE_TYPE,
        get_resource=backend.get_network,
        find_by_name=backend.find_network_by_name,
        create_resource=backend.create_network,
    )


def _resolve_optional_service(
    *,
    dry_run: bool,
    ownership_ledger: OwnershipLedger,
    service_name: str | None,
    resource_type: str,
    scope: str,
    get_resource: Callable[[str], SharedCoreResourceRecord | None],
    find_by_name: Callable[[str], SharedCoreResourceRecord | None],
    create_resource: Callable[[str], SharedCoreResourceRecord],
) -> tuple[SharedCoreManagedResource | None, str | None]:
    if service_name is None:
        return None, None
    return _resolve_resource(
        dry_run=dry_run,
        service_name=service_name,
        owned_resource=_find_owned_resource(
            ownership_ledger=ownership_ledger,
            resource_type=resource_type,
            scope=scope,
        ),
        resource_type=resource_type,
        get_resource=get_resource,
        find_by_name=find_by_name,
        create_resource=create_resource,
    )


def _resolve_resource(
    *,
    dry_run: bool,
    service_name: str,
    owned_resource: OwnedResource | None,
    resource_type: str,
    get_resource: Callable[[str], SharedCoreResourceRecord | None],
    find_by_name: Callable[[str], SharedCoreResourceRecord | None],
    create_resource: Callable[[str], SharedCoreResourceRecord],
) -> tuple[SharedCoreManagedResource, str]:
    if owned_resource is not None:
        existing = get_resource(owned_resource.resource_id)
        if existing is None:
            raise SharedCoreError(
                f"Ownership ledger says shared-core resource '{resource_type}' exists, "
                "but the backend could not find it."
            )
        if existing.resource_name != service_name:
            raise SharedCoreError(
                f"Ownership ledger resource '{resource_type}' no longer matches the desired "
                "shared-core naming convention."
            )
        return (
            SharedCoreManagedResource(
                action="reuse_owned",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    existing = find_by_name(service_name)
    if existing is not None:
        return (
            SharedCoreManagedResource(
                action="reuse_existing",
                resource_id=existing.resource_id,
                resource_name=existing.resource_name,
            ),
            existing.resource_id,
        )

    if dry_run:
        return (
            SharedCoreManagedResource(
                action="create",
                resource_id=f"planned:{service_name}",
                resource_name=service_name,
            ),
            f"planned:{service_name}",
        )

    created = create_resource(service_name)
    return (
        SharedCoreManagedResource(
            action="create",
            resource_id=created.resource_id,
            resource_name=created.resource_name,
        ),
        created.resource_id,
    )


def _derive_outcome(actions: tuple[str, ...]) -> str:
    if "create" in actions:
        return "applied"
    return "already_present"


def _find_owned_resource(
    ownership_ledger: OwnershipLedger,
    resource_type: str,
    scope: str,
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == resource_type and resource.scope == scope
    ]
    if len(matches) > 1:
        raise SharedCoreError(
            f"Ownership ledger contains multiple '{resource_type}' resources for scope '{scope}'."
        )
    return matches[0] if matches else None


def _network_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:shared-network"


def _postgres_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:shared-postgres"


def _redis_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:shared-redis"


def _mail_relay_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:shared-postfix"
