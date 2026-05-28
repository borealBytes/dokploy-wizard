"""Deterministic shared-core planning derived from selected packs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dokploy_wizard.core.models import (
    PackSharedAllocation,
    SharedCorePlan,
    SharedLiteLLMServicePlan,
    SharedMailRelayServicePlan,
    SharedPostgresAllocation,
    SharedPostgresServicePlan,
    SharedRedisAllocation,
    SharedRedisServicePlan,
)
from dokploy_wizard.packs.catalog import get_pack_definition
from dokploy_wizard.packs.env_metadata import build_pack_env_specs as _build_pack_env_specs

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.env_spec import DokployEnvSpec

_DEFAULT_LITELLM_ALIAS_ORDER = (
    "opencode-go/deepseek-v4-flash",
    # PAUSED: OpenCode Go route — will re-enable later.
    # "opencode-go/*",
    # PAUSED: OpenRouter route — will re-enable later.
    # "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
)
DEFAULT_LOCAL_CANONICAL_ALIAS = "local-model.internal/unsloth-active"


def build_shared_core_plan(
    stack_name: str,
    enabled_packs: tuple[str, ...],
    values: dict[str, str] | None = None,
) -> SharedCorePlan:
    allocations: list[PackSharedAllocation] = []
    litellm_postgres = SharedPostgresAllocation(
        database_name=f"{stack_name}_litellm".replace("-", "_"),
        user_name=f"{stack_name}_litellm".replace("-", "_")[:63],
        password_secret_ref=f"{stack_name}-litellm-postgres-password",
    )

    requires_postgres = True
    requires_redis = False
    values = values or {}

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
        mail_relay=_build_shared_mail_relay_plan(stack_name, enabled_packs, values),
        litellm=SharedLiteLLMServicePlan(
            service_name=f"{stack_name}-shared-litellm",
            postgres=litellm_postgres,
            default_model_alias_order=_build_litellm_default_alias_order(values),
        ),
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


def _build_litellm_default_alias_order(values: dict[str, str]) -> tuple[str, ...]:
    aliases: list[str] = []
    ai_default_alias = _ai_default_alias(values)

    if ai_default_alias is not None and _alias_has_active_upstream(ai_default_alias, values):
        aliases.append(ai_default_alias)

    if _litellm_local_upstream_configured(values):
        aliases.append(_local_alias(values))

    if _litellm_openrouter_upstream_configured(values):
        aliases.extend(alias for alias, _target in _parse_litellm_openrouter_models(values))

    return tuple(dict.fromkeys(aliases)) or _DEFAULT_LITELLM_ALIAS_ORDER


def _ai_default_alias(values: dict[str, str]) -> str | None:
    provider = _optional_value(values, "AI_DEFAULT_PROVIDER")
    model = _optional_value(values, "AI_DEFAULT_MODEL")
    if provider is None or model is None:
        return None
    normalized_provider = _canonical_ai_default_provider(provider)
    if normalized_provider == "local":
        local_provider, _, _ = DEFAULT_LOCAL_CANONICAL_ALIAS.partition("/")
        normalized_provider = local_provider
    if model.startswith(f"{normalized_provider}/"):
        return model
    return f"{normalized_provider}/{model}"


def _alias_has_active_upstream(alias: str, values: dict[str, str]) -> bool:
    provider_slug, _, _ = alias.partition("/")
    local_provider, _, _ = DEFAULT_LOCAL_CANONICAL_ALIAS.partition("/")
    if provider_slug == local_provider or "." in provider_slug:
        return _litellm_local_upstream_configured(values)
    if provider_slug == "openrouter":
        return _litellm_openrouter_upstream_configured(values)
    if provider_slug == "opencode-go":
        return _optional_value(values, "LITELLM_OPENCODE_GO_API_KEY") is not None
    return True


def _litellm_local_upstream_configured(values: dict[str, str]) -> bool:
    return (
        _optional_value(values, "LITELLM_LOCAL_BASE_URL") is not None
        and _optional_value(values, "LITELLM_LOCAL_MODEL") is not None
        and _optional_value(values, "LITELLM_LOCAL_API_KEY") is not None
    )


def _local_alias(values: dict[str, str]) -> str:
    ai_default_alias = _ai_default_alias(values)
    if ai_default_alias is None:
        return DEFAULT_LOCAL_CANONICAL_ALIAS
    provider_slug, _, _ = ai_default_alias.partition("/")
    local_provider, _, _ = DEFAULT_LOCAL_CANONICAL_ALIAS.partition("/")
    if provider_slug == local_provider or "." in provider_slug:
        return ai_default_alias
    return DEFAULT_LOCAL_CANONICAL_ALIAS


def _litellm_openrouter_upstream_configured(values: dict[str, str]) -> bool:
    return (
        _optional_value(values, "LITELLM_OPENROUTER_API_KEY") is not None
        and bool(_parse_litellm_openrouter_models(values))
    )


def _parse_litellm_openrouter_models(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    raw_value = _optional_value(values, "LITELLM_OPENROUTER_MODELS")
    if raw_value is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in raw_value.split(","):
        normalized_item = item.strip()
        if normalized_item == "":
            continue
        alias, separator, target_model = normalized_item.partition("=")
        if separator == "=":
            normalized_alias = _normalize_openrouter_ref(alias.strip())
            normalized_target = _normalize_openrouter_ref(target_model.strip())
        else:
            normalized_alias = _normalize_openrouter_ref(normalized_item)
            normalized_target = normalized_alias
        if normalized_alias and normalized_target:
            pairs.append((normalized_alias, normalized_target))
    return tuple(pairs)


def _normalize_openrouter_ref(model_ref: str) -> str:
    if model_ref == "" or model_ref.startswith("openrouter/"):
        return model_ref
    return f"openrouter/{model_ref}"


def _canonical_ai_default_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    aliases = {"opencode": "opencode-go"}
    return aliases.get(normalized, normalized)


def _optional_value(values: dict[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_pack_env_specs(
    stack_name: str,
    enabled_packs: tuple[str, ...],
    values: dict[str, str],
) -> tuple[DokployEnvSpec, ...]:
    """Build explicit pack env specs for present mutable env values."""

    return _build_pack_env_specs(
        stack_name=stack_name,
        enabled_packs=enabled_packs,
        values=values,
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
