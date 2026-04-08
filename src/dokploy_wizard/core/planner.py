"""Deterministic shared-core planning derived from selected packs."""

from __future__ import annotations

from dokploy_wizard.core.models import (
    PackSharedAllocation,
    SharedCorePlan,
    SharedPostgresAllocation,
    SharedPostgresServicePlan,
    SharedRedisAllocation,
    SharedRedisServicePlan,
)
from dokploy_wizard.packs.catalog import get_pack_definition


def build_shared_core_plan(stack_name: str, enabled_packs: tuple[str, ...]) -> SharedCorePlan:
    allocations: list[PackSharedAllocation] = []
    requires_postgres = False
    requires_redis = False

    for pack_name in enabled_packs:
        requirements = get_pack_definition(pack_name).shared_core_requirements
        postgres = None
        redis = None
        if "postgres" in requirements:
            requires_postgres = True
            postgres = SharedPostgresAllocation(
                database_name=f"{stack_name}_{pack_name}".replace("-", "_"),
                user_name=f"{stack_name}_{pack_name}".replace("-", "_")[:63],
                password_secret_ref=f"{stack_name}-{pack_name}-postgres-password",
            )
        if "redis" in requirements:
            requires_redis = True
            redis = SharedRedisAllocation(
                identity_name=f"{stack_name}-{pack_name}-redis",
                password_secret_ref=f"{stack_name}-{pack_name}-redis-password",
            )
        if postgres is not None or redis is not None:
            allocations.append(
                PackSharedAllocation(
                    pack_name=pack_name,
                    network_alias=pack_name,
                    postgres=postgres,
                    redis=redis,
                )
            )

    return SharedCorePlan(
        network_name=f"{stack_name}-shared",
        postgres=(
            None
            if not requires_postgres
            else SharedPostgresServicePlan(service_name=f"{stack_name}-shared-postgres")
        ),
        redis=(
            None
            if not requires_redis
            else SharedRedisServicePlan(service_name=f"{stack_name}-shared-redis")
        ),
        allocations=tuple(allocations),
    )
