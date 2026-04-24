"""Deterministic shared-core planning derived from selected packs."""

from __future__ import annotations

from dokploy_wizard.core.models import (
    PackSharedAllocation,
    SharedCorePlan,
    SharedMailRelayServicePlan,
    SharedPostgresAllocation,
    SharedPostgresServicePlan,
    SharedRedisAllocation,
    SharedRedisServicePlan,
)
from dokploy_wizard.packs.catalog import get_pack_definition


def build_shared_core_plan(
    stack_name: str,
    enabled_packs: tuple[str, ...],
    values: dict[str, str] | None = None,
) -> SharedCorePlan:
    allocations: list[PackSharedAllocation] = []
    requires_postgres = False
    requires_redis = False
    values = values or {}
    postgres_major_version: int | None = None
    postgres_image: str | None = None
    available_extensions: tuple[str, ...] = ()

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
                minimum_postgres_major_version=(17 if pack_name == "multica" else None),
                required_extensions=(("vector",) if pack_name == "multica" else ()),
            )
            if pack_name == "multica":
                postgres_major_version = 17
                postgres_image = "pgvector/pgvector:pg17"
                available_extensions = ("vector",)
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
        mail_relay=_build_shared_mail_relay_plan(stack_name, enabled_packs, values),
        postgres=(
            None
            if not requires_postgres
            else SharedPostgresServicePlan(
                service_name=f"{stack_name}-shared-postgres",
                image=postgres_image,
                major_version=postgres_major_version,
                available_extensions=available_extensions,
            )
        ),
        redis=(
            None
            if not requires_redis
            else SharedRedisServicePlan(service_name=f"{stack_name}-shared-redis")
        ),
        allocations=tuple(allocations),
    )


def _build_shared_mail_relay_plan(
    stack_name: str,
    enabled_packs: tuple[str, ...],
    values: dict[str, str],
) -> SharedMailRelayServicePlan | None:
    if not ({"moodle", "docuseal"} & set(enabled_packs)):
        return None
    root_domain = values.get("ROOT_DOMAIN", "").strip()
    if root_domain == "":
        return None
    mail_hostname = values.get("OUTBOUND_SMTP_HOSTNAME", f"mail.{root_domain}").strip()
    from_address = values.get("OUTBOUND_SMTP_FROM_ADDRESS", f"DoNotReply@{root_domain}").strip()
    return SharedMailRelayServicePlan(
        service_name=f"{stack_name}-shared-postfix",
        mail_hostname=mail_hostname,
        smtp_port=587,
        from_address=from_address,
    )
