from __future__ import annotations

from dokploy_wizard.core.models import SharedCorePlan
from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretSpec
from dokploy_wizard.dokploy.shared_core import (
    _build_litellm_consumer_projection_catalog,
    _build_litellm_upstream_creds,
)
from dokploy_wizard.litellm.config_renderer import build_litellm_config

CODER_SECRET_NAMES = frozenset(
    (
        "hermes-inference-provider",
        "hermes-model",
        "hermes-openai-api-base",
        "hermes-openai-api-key",
        "kdense-litellm-api-key",
    )
)


def build_coder_secret_specs(
    *,
    stack_name: str,
    default_alias: str,
    visible_aliases: tuple[str, ...],
    coder_hermes_key: str,
    coder_kdense_key: str,
) -> tuple[CoderSecretSpec, ...]:
    if not all((stack_name, default_alias, coder_hermes_key, coder_kdense_key)):
        raise ValueError("Coder secret values are invalid")
    if default_alias not in visible_aliases:
        raise ValueError("Coder default model is absent from the visible LiteLLM catalog")
    specs = (
        CoderSecretSpec(
            "hermes-inference-provider", "HERMES_INFERENCE_PROVIDER", "openai", "Hermes provider"
        ),
        CoderSecretSpec("hermes-model", "HERMES_MODEL", default_alias, "Hermes model"),
        CoderSecretSpec(
            "hermes-openai-api-base",
            "OPENAI_API_BASE",
            f"http://{stack_name}-shared-litellm:4000/v1",
            "Hermes LiteLLM base URL",
        ),
        CoderSecretSpec(
            "hermes-openai-api-key",
            "OPENAI_API_KEY",
            coder_hermes_key,
            "Hermes LiteLLM key",
        ),
        CoderSecretSpec(
            "kdense-litellm-api-key",
            "KDENSE_LITELLM_API_KEY",
            coder_kdense_key,
            "K-Dense LiteLLM key",
        ),
    )
    if frozenset(spec.name for spec in specs) != CODER_SECRET_NAMES:
        raise ValueError("Coder secret specification set is invalid")
    return specs


def build_coder_visible_litellm_aliases(
    *, flat_env: dict[str, str], plan: SharedCorePlan
) -> tuple[str, ...]:
    if plan.litellm is None:
        raise ValueError("Coder secret aliases require an active LiteLLM plan")
    config = build_litellm_config(flat_env, _build_litellm_upstream_creds(flat_env))
    projection = _build_litellm_consumer_projection_catalog(
        flat_env=flat_env,
        config=config,
        plan=plan,
    )
    aliases = projection.visible_aliases_for("coder-hermes")
    if not aliases:
        raise ValueError("Coder secret aliases are unavailable from the LiteLLM projection")
    return aliases
