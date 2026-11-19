"""LiteLLM gateway model autosync.

The autosync sidecar runs beside the shared-core LiteLLM service, polls the
configured model catalog sources (OpenRouter, OpenCode Zen, NVIDIA, and the
operator's own OpenAI-compatible endpoint), and registers newly published
models through the LiteLLM admin API. Each cycle
also refreshes consumer virtual-key allowlists with the explicit sync/* set,
because /v1/models does not expand the sync/* wildcard and workspace tooling
seeds its model picker from that endpoint. Synced aliases are provider-qualified
and flat — sync/<provider>-<model-id> with the id's slashes written as "->" —
so surfaces that render only the last path segment of a model id still show
which provider's bill an alias touches, and each alias records the provider's
per-token cost wherever a catalog prices the model. This module renders the
sidecar's source document, exposes the shared gating rules, and loads the
sidecar script text that is mounted into the container.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

MODEL_SYNC_ALIAS_PREFIX = "sync"
MODEL_SYNC_CONSUMER_WILDCARD = "sync/*"
MODEL_SYNC_MANAGED_BY = "dokploy-wizard-model-sync"
MODEL_SYNC_DEFAULT_INTERVAL_MINUTES = 360
MODEL_SYNC_MIN_INTERVAL_MINUTES = 5
MODEL_SYNC_DEFAULT_MAX_MODELS_PER_PROVIDER = 50
MODEL_SYNC_DEFAULT_PROVIDERS = ("openrouter", "opencode-go", "nvidia", "local")
# One stable alias for "the model an agent gets when the operator did not name one".
# The provider half stays an explicit operator choice; only the model is picked, from
# the live catalog, by pattern. That way a rename upstream costs a sync cycle instead
# of a stack rebuild, while a stack that wants a specific model still names one.
MODEL_SYNC_DEFAULT_CHAT_ALIAS = "sync/default-chat"
MODEL_SYNC_DEFAULT_CHAT_PATTERN_ENV_KEY = "AI_DEFAULT_MODEL_PATTERN"
MODEL_SYNC_DEFAULT_CHAT_PATTERN = "deepseek*flash*"
# AI_DEFAULT_PROVIDER values that map onto a sync source; anything else (a local
# endpoint, an unsupported provider) leaves the alias uncreated.
_DEFAULT_CHAT_PROVIDER_SOURCES = {
    "openrouter": "openrouter",
    "opencode-go": "opencode-go",
    "nvidia": "nvidia",
}
_DEFAULT_AI_DEFAULT_PROVIDER = "opencode-go"

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
_OPENROUTER_CHAT_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_RANKINGS_URL = "https://openrouter.ai/api/frontend/v1/rankings/models"
# OpenRouter registrations track the public weekly leaderboard: the top paid
# (standard-variant) models plus the top free-variant models, capped so the
# picker stays human-scale instead of a 400-model firehose.
MODEL_SYNC_OPENROUTER_MAX_MODELS = 30
MODEL_SYNC_OPENROUTER_MAX_RANKED_PAID = 20
MODEL_SYNC_OPENROUTER_MAX_RANKED_FREE = 10
# OpenCode Go is consumed in full whenever a key is configured: the catalog is
# curated upstream and small, so no ranking slice or env model list applies.
MODEL_SYNC_OPENCODE_GO_MAX_MODELS = 100
_OPENCODE_GO_BASE_URL_DEFAULT = "https://opencode.ai/zen/go/v1"
_NVIDIA_BASE_URL_DEFAULT = "https://integrate.api.nvidia.com/v1"
# NVIDIA's /v1/models list is "servable" rather than "live": it contains
# delisted models that 404 on chat completion and embedding/utility models
# that never answer a chat probe. The sidecar therefore liveness-probes the
# NVIDIA catalog with a 1-token chat completion (never paid OpenRouter-style
# traffic) and keeps only the top slice that answers, capped so the picker
# stays human-scale. Probe verdicts are cached for a week; dead models are
# re-probed every cycle.
MODEL_SYNC_NVIDIA_MAX_MODELS_DEFAULT = 20
# A personal endpoint serves what the operator loaded, usually one model at a
# time; the listing shape is the OpenAI one, so no probe or ranking slice is
# needed. The cap only guards against a runaway /v1/models listing.
MODEL_SYNC_LOCAL_MAX_MODELS = 50
# Placeholder key handed to LiteLLM for a local endpoint configured without a
# key. The same convention the static local alias uses; OpenAI-compatible
# servers that do not check auth (sglang, vLLM default, Ollama) discard it.
MODEL_SYNC_LOCAL_KEYLESS_API_KEY = "sk-no-key-required"
MODEL_SYNC_DEFAULT_PROBE_TTL_DAYS = 7
# models.dev is the registry OpenCode itself consumes: per-model pricing in
# the same org/model id form the provider catalogs use. The sync consumes it
# twice over — as the zero-cost gate that keeps paid NVIDIA endpoints out of
# the picker (and off the chat probe that would bill them), and as the price
# catalog behind every synced alias, so operator-facing cost metadata comes
# from the same registry the free-tier claim is made with.
_MODELS_DEV_CATALOG_URL = "https://models.dev/api.json"


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


def model_sync_openrouter_probe_enabled(flat_env: Mapping[str, str]) -> bool:
    """Whether to chat-probe OpenRouter's free ranked models before listing them.

    Free rows are the ones providers delist out from under a stack, and a delisted
    alias still shows in the picker and then fails on the first message. Only free
    rows are probed, so the check never costs a paid completion.
    """

    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_OPENROUTER_PROBE")
    if raw is None:
        return True
    return raw.strip().lower() not in _MODEL_SYNC_DISABLED_VALUES


def model_sync_nvidia_probe_enabled(flat_env: Mapping[str, str]) -> bool:
    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_NVIDIA_PROBE")
    if raw is None:
        return True
    return raw.strip().lower() not in _MODEL_SYNC_DISABLED_VALUES


def model_sync_nvidia_free_only(flat_env: Mapping[str, str]) -> bool:
    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_NVIDIA_FREE_ONLY")
    if raw is None:
        return True
    return raw.strip().lower() not in _MODEL_SYNC_DISABLED_VALUES


def model_sync_nvidia_max_models(flat_env: Mapping[str, str]) -> int:
    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_NVIDIA_MAX_MODELS")
    if raw is None:
        return MODEL_SYNC_NVIDIA_MAX_MODELS_DEFAULT
    try:
        parsed = int(raw)
    except ValueError:
        return MODEL_SYNC_NVIDIA_MAX_MODELS_DEFAULT
    return max(1, parsed)


def model_sync_default_chat_pattern(flat_env: Mapping[str, str]) -> str | None:
    """The pattern sync/default-chat must match, or None when the alias is off.

    Unset takes the shipped default. An explicit empty value turns the alias off.
    A pattern with no literal text ("*", "**") is treated as off too: it matches the
    whole catalog, which means it selects nothing and the name would lie.
    """

    raw = flat_env.get(MODEL_SYNC_DEFAULT_CHAT_PATTERN_ENV_KEY)
    pattern = MODEL_SYNC_DEFAULT_CHAT_PATTERN if raw is None else raw.strip()
    if not pattern:
        return None
    if not any(segment.strip() for segment in pattern.split("*")):
        return None
    return pattern


def build_default_chat_config(flat_env: Mapping[str, str]) -> dict[str, str] | None:
    """The sidecar's default-chat block for the provider the operator chose."""

    provider = (
        _optional(flat_env, "AI_DEFAULT_PROVIDER") or _DEFAULT_AI_DEFAULT_PROVIDER
    ).strip().lower()
    source_name = _DEFAULT_CHAT_PROVIDER_SOURCES.get(provider)
    if source_name is None:
        return None
    pattern = model_sync_default_chat_pattern(flat_env)
    if pattern is None:
        return None
    return {
        "pattern": pattern,
        "alias": MODEL_SYNC_DEFAULT_CHAT_ALIAS,
        "source": source_name,
    }


def model_sync_default_chat_alias(
    flat_env: Mapping[str, str], upstream_creds: Mapping[str, object]
) -> str | None:
    """The default-chat alias to pre-authorize on consumer keys, when it can exist.

    Consumer allowlists are explicit names, so an alias a key does not list is
    invisible to that consumer until the sidecar's first cycle merges it in. Listing
    it at deploy time removes that window, and it is only listed when the provider it
    would point at is actually being synced.
    """

    config = build_default_chat_config(flat_env)
    if config is None:
        return None
    if not model_sync_enabled(flat_env, upstream_creds):
        return None
    sources = build_model_sync_sources(flat_env, upstream_creds)
    synced = {str(source.get("name")) for source in sources}
    return config["alias"] if config["source"] in synced else None


def model_sync_probe_ttl_days(flat_env: Mapping[str, str]) -> int:
    raw = _optional(flat_env, "LITELLM_MODEL_SYNC_PROBE_TTL_DAYS")
    if raw is None:
        return MODEL_SYNC_DEFAULT_PROBE_TTL_DAYS
    try:
        parsed = int(raw)
    except ValueError:
        return MODEL_SYNC_DEFAULT_PROBE_TTL_DAYS
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
                    "list_url": _OPENROUTER_CATALOG_URL,
                    "rankings_url": _OPENROUTER_RANKINGS_URL,
                    "api_key_env": openrouter_api_key_env,
                    "model_ref_template": "openrouter/{model}",
                    "max_models": MODEL_SYNC_OPENROUTER_MAX_MODELS,
                    "max_ranked_paid": MODEL_SYNC_OPENROUTER_MAX_RANKED_PAID,
                    "max_ranked_free": MODEL_SYNC_OPENROUTER_MAX_RANKED_FREE,
                    # Probe base, not api_base: the aliases are routed by LiteLLM's
                    # provider prefix and must keep doing so.
                    "probe_base": _OPENROUTER_CHAT_BASE_URL,
                    "probe": model_sync_openrouter_probe_enabled(flat_env),
                    "probe_free_only": True,
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
                    "list_url": f"{base_url}/models",
                    "api_key_env": opencode_go_api_key_env,
                    "api_base": base_url,
                    "model_ref_template": "openai/{model}",
                    "max_models": MODEL_SYNC_OPENCODE_GO_MAX_MODELS,
                    "extra_headers": dict(OPENCODE_GO_GATEWAY_EXTRA_HEADERS),
                    # The gateway's own catalog rows carry no pricing, so the
                    # sidecar reads provider costs from models.dev, which keys
                    # OpenCode Go models by the same ids.
                    "cost_catalog_url": _MODELS_DEV_CATALOG_URL,
                    "cost_catalog_provider": "opencode-go",
                }
            )

    if "nvidia" in providers:
        nvidia_api_key_env = _api_key_env(flat_env, upstream_creds, "nvidia_api_key_env")
        if nvidia_api_key_env is not None:
            base_url = _nvidia_base_url(flat_env)
            sources.append(
                {
                    "name": "nvidia",
                    "list_url": f"{base_url}/models",
                    "api_key_env": nvidia_api_key_env,
                    "api_base": base_url,
                    "model_ref_template": "openai/{model}",
                    "max_models": model_sync_nvidia_max_models(flat_env),
                    "probe": model_sync_nvidia_probe_enabled(flat_env),
                    "probe_ttl_days": model_sync_probe_ttl_days(flat_env),
                    "free_only": model_sync_nvidia_free_only(flat_env),
                    # Doubles as the zero-cost gate (free_only) and the price
                    # catalog for the cost metadata every alias carries.
                    "cost_catalog_url": _MODELS_DEV_CATALOG_URL,
                    "cost_catalog_provider": "nvidia",
                    # Free-verified aliases get a ":free" tail so pickers show
                    # what the model actually costs the operator: nothing.
                    "alias_suffix": ":free",
                }
            )

    if "local" in providers:
        local_base_url = _optional(flat_env, "LITELLM_LOCAL_BASE_URL")
        if local_base_url is not None:
            # The endpoint's own /v1/models is the catalog: whatever the operator
            # is serving right now, exposed by the name it is served under, rescanned
            # every cycle like any other provider. No static model id to configure.
            # The source is named for the host that serves (its tailnet label), not a
            # generic "local", so a picker shows which machine answers each alias.
            source: dict[str, object] = {
                "name": _local_source_name(local_base_url),
                "list_url": f"{local_base_url.rstrip('/')}/models",
                "api_base": local_base_url.rstrip("/"),
                "model_ref_template": "openai/{model}",
                "max_models": MODEL_SYNC_LOCAL_MAX_MODELS,
                # Same posture as the static local alias: local chat templates
                # can reject system messages that do not lead the history, so the
                # gateway folds system content into user content.
                "litellm_params_extra": {"supports_system_message": False},
            }
            if _optional(flat_env, "LITELLM_LOCAL_API_KEY") is not None:
                source["api_key_env"] = "LITELLM_LOCAL_API_KEY"
            else:
                source["default_api_key"] = MODEL_SYNC_LOCAL_KEYLESS_API_KEY
            sources.append(source)

    default_chat = build_default_chat_config(flat_env)
    if default_chat is not None:
        sources = [
            {
                **source,
                "default_chat": {
                    "pattern": default_chat["pattern"],
                    "alias": default_chat["alias"],
                },
            }
            if source.get("name") == default_chat["source"]
            else source
            for source in sources
        ]

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


def _local_source_name(base_url: str) -> str:
    """Source alias for a local endpoint: the first label of its host.

    tuxdesktop.felis-aeolian.ts.net names the machine, not the tailnet or the
    port, and the label survives MagicDNS suffix churn. A bare IP keeps its
    full dotted form; anything that cannot be a host falls back to "local".
    """

    host = urlparse(base_url).hostname or ""
    label = host.split(".")[0] if not _looks_numeric(host) else host
    label = re.sub(r"[^a-z0-9_.-]+", "-", label.lower()).strip("-.")
    return label or "local"


def _looks_numeric(host: str) -> bool:
    return all(part.isdigit() for part in host.split(".")) if host else False


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
