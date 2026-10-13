"""LiteLLM gateway model autosync.

The autosync sidecar runs beside the shared-core LiteLLM service, polls the
configured model catalog sources (OpenRouter, OpenCode Zen, NVIDIA), and
registers newly published models through the LiteLLM admin API. This module
renders the sidecar's source document, exposes the shared gating rules, and
loads the sidecar script text that is mounted into the container.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

MODEL_SYNC_ALIAS_PREFIX = "sync"
MODEL_SYNC_CONSUMER_WILDCARD = "sync/*"
MODEL_SYNC_MANAGED_BY = "dokploy-wizard-model-sync"
MODEL_SYNC_DEFAULT_INTERVAL_MINUTES = 360
MODEL_SYNC_MIN_INTERVAL_MINUTES = 5
MODEL_SYNC_DEFAULT_MAX_MODELS_PER_PROVIDER = 50
MODEL_SYNC_DEFAULT_PROVIDERS = ("openrouter", "opencode-go", "nvidia")

# OpenCode Go rejects requests without a stable session id (x-opencode-session)
# and a non-generic user agent. The gateway injects both on every OpenCode Go
# deployment so all consumers route successfully; clients that send their own
# session header have it forwarded per-request (see config_renderer).
OPENCODE_GO_SESSION_HEADER = "x-opencode-session"
OPENCODE_GO_GATEWAY_SESSION_ID = "dokploy-wizard-shared-gateway"
OPENCODE_GO_GATEWAY_USER_AGENT = "dokploy-wizard-litellm/1.0"
OPENCODE_GO_GATEWAY_EXTRA_HEADERS: dict[str, str] = {
    OPENCODE_GO_SESSION_HEADER: OPENCODE_GO_GATEWAY_SESSION_ID,
    "User-Agent": OPENCODE_GO_GATEWAY_USER_AGENT,
}

_MODEL_SYNC_DISABLED_VALUES = {"0", "false", "no", "off"}
_MODEL_SYNC_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "templates" / "litellm" / "model-sync"
)
_OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"
_OPENCODE_GO_BASE_URL_DEFAULT = "https://opencode.ai/zen/go/v1"
_NVIDIA_BASE_URL_DEFAULT = "https://integrate.api.nvidia.com/v1"


def model_sync_service_name(litellm_service_name: str) -> str:
    return f"{litellm_service_name}-sync"


def model_sync_enabled(flat_env: Mapping[str, str], upstream_creds: Mapping[str, object]) -> bool:
    override = _optional(flat_env, "LITELLM_MODEL_SYNC_ENABLED")
    if override is not None and override.lower() in _MODEL_SYNC_DISABLED_VALUES:
        return False
    return bool(build_model_sync_sources(flat_env, upstream_creds))


def model_sync_interval_minutes(flat_env: Mapping[str, str]) -> int:
    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_INTERVAL_MINUTES")
    if raw is None:
        return MODEL_SYNC_DEFAULT_INTERVAL_MINUTES
    try:
        parsed = int(raw)
    except ValueError:
        return MODEL_SYNC_DEFAULT_INTERVAL_MINUTES
    return max(MODEL_SYNC_MIN_INTERVAL_MINUTES, parsed)


def model_sync_max_models_per_provider(flat_env: Mapping[str, str]) -> int:
    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_MAX_MODELS")
    if raw is None:
        return MODEL_SYNC_DEFAULT_MAX_MODELS_PER_PROVIDER
    try:
        parsed = int(raw)
    except ValueError:
        return MODEL_SYNC_DEFAULT_MAX_MODELS_PER_PROVIDER
    return max(1, parsed)


def build_model_sync_sources(
    flat_env: Mapping[str, str], upstream_creds: Mapping[str, object]
) -> tuple[dict[str, object], ...]:
    providers = _selected_providers(flat_env)
    sources: list[dict[str, object]] = []

    if "openrouter" in providers:
        openrouter_api_key_env = _api_key_env(flat_env, upstream_creds, "openrouter_api_key_env")
        if openrouter_api_key_env is not None:
            sources.append(
                {
                    "name": "openrouter",
                    "alias_prefix": f"{MODEL_SYNC_ALIAS_PREFIX}/openrouter",
                    "list_url": _OPENROUTER_CATALOG_URL,
                    "api_key_env": openrouter_api_key_env,
                    "model_ref_template": "openrouter/{model}",
                }
            )

    if "opencode-go" in providers:
        opencode_go_api_key_env = _api_key_env(
            flat_env, upstream_creds, "opencode_go_api_key_env"
        )
        if opencode_go_api_key_env is not None:
            base_url = _opencode_go_base_url(flat_env)
            sources.append(
                {
                    "name": "opencode-go",
                    "alias_prefix": f"{MODEL_SYNC_ALIAS_PREFIX}/opencode-go",
                    "list_url": f"{base_url}/models",
                    "api_key_env": opencode_go_api_key_env,
                    "api_base": base_url,
                    "model_ref_template": "openai/{model}",
                    "extra_headers": dict(OPENCODE_GO_GATEWAY_EXTRA_HEADERS),
                }
            )

    if "nvidia" in providers:
        nvidia_api_key_env = _api_key_env(flat_env, upstream_creds, "nvidia_api_key_env")
        if nvidia_api_key_env is not None:
            base_url = _nvidia_base_url(flat_env)
            sources.append(
                {
                    "name": "nvidia",
                    "alias_prefix": f"{MODEL_SYNC_ALIAS_PREFIX}/nvidia",
                    "list_url": f"{base_url}/models",
                    "api_key_env": nvidia_api_key_env,
                    "api_base": base_url,
                    "model_ref_template": "openai/{model}",
                }
            )

    return tuple(sources)


def render_model_sync_sources_document(
    flat_env: Mapping[str, str], upstream_creds: Mapping[str, object]
) -> str:
    document = {
        "interval_minutes": model_sync_interval_minutes(flat_env),
        "max_models_per_provider": model_sync_max_models_per_provider(flat_env),
        "sources": [dict(source) for source in build_model_sync_sources(flat_env, upstream_creds)],
    }
    return json.dumps(document, indent=2) + "\n"


def model_sync_script_text() -> str:
    return (_MODEL_SYNC_TEMPLATE_DIR / "sync.py").read_text(encoding="utf-8")


def _selected_providers(flat_env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_PROVIDERS")
    if raw is None:
        return MODEL_SYNC_DEFAULT_PROVIDERS
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _api_key_env(
    flat_env: Mapping[str, str], upstream_creds: Mapping[str, object], cred_key: str
) -> str | None:
    value = upstream_creds.get(cred_key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    legacy = {
        "openrouter_api_key_env": "OPENROUTER_API_KEY",
        "opencode_go_api_key_env": "OPENCODE_GO_API_KEY",
        "nvidia_api_key_env": "NVIDIA_API_KEY",
    }.get(cred_key)
    if legacy is not None:
        canonical = {
            "openrouter_api_key_env": "LITELLM_OPENROUTER_API_KEY",
            "opencode_go_api_key_env": "LITELLM_OPENCODE_GO_API_KEY",
            "nvidia_api_key_env": "LITELLM_NVIDIA_API_KEY",
        }[cred_key]
        if _optional(flat_env, legacy) is not None and _optional(flat_env, canonical) is None:
            return legacy
    return None


def _opencode_go_base_url(flat_env: Mapping[str, str]) -> str:
    return (
        _optional(flat_env, "AI_DEFAULT_BASE_URL")
        or _optional(flat_env, "OPENCODE_GO_BASE_URL")
        or _OPENCODE_GO_BASE_URL_DEFAULT
    ).rstrip("/")


def _nvidia_base_url(flat_env: Mapping[str, str]) -> str:
    return (
        _optional(flat_env, "LITELLM_NVIDIA_BASE_URL")
        or _optional(flat_env, "NVIDIA_BASE_URL")
        or _NVIDIA_BASE_URL_DEFAULT
    ).rstrip("/")


def _optional(flat_env: Mapping[str, str], key: str) -> str | None:
    value = flat_env.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
