from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_LOCAL_ALIAS = "local/unsloth-active"
DEFAULT_LOCAL_MODEL = "openai/unsloth-active"
DEFAULT_LOCAL_API_KEY = "sk-no-key-required"
DEFAULT_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"

_VERIFIED_OPENCODE_GO_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek-v4-flash",
        "litellm_model": "openai/deepseek-v4-flash",
        "chat_compatible": True,
    },
    {
        "id": "gpt-4.1-mini",
        "litellm_model": "openai/gpt-4.1-mini",
        "chat_compatible": True,
    },
    {
        "id": "text-embedding-3-large",
        "litellm_model": "openai/text-embedding-3-large",
        "chat_compatible": False,
    },
)


def build_litellm_config(
    flat_env: Mapping[str, str], upstream_creds: Mapping[str, object]
) -> dict[str, object]:
    model_list: list[dict[str, object]] = []

    local_base_url = _optional(flat_env, "LITELLM_LOCAL_BASE_URL")
    if local_base_url is not None:
        local_alias = _local_alias(flat_env)
        local_model = _normalize_local_model_ref(
            _optional(flat_env, "LITELLM_LOCAL_MODEL")
            or _optional(flat_env, "AI_DEFAULT_MODEL")
            or DEFAULT_LOCAL_MODEL
        )
        local_api_key = _optional(flat_env, "LITELLM_LOCAL_API_KEY") or DEFAULT_LOCAL_API_KEY
        model_list.append(
            {
                "model_name": local_alias,
                "litellm_params": {
                    "model": local_model,
                    "api_base": local_base_url,
                    "api_key": local_api_key,
                },
            }
        )

    opencode_go_api_key_env = _optional_env_name(upstream_creds, "opencode_go_api_key_env")
    has_explicit_opencode_go_base_url = any(
        _optional(flat_env, key) is not None
        for key in ("AI_DEFAULT_BASE_URL", "OPENCODE_GO_BASE_URL")
    )
    if opencode_go_api_key_env is not None and has_explicit_opencode_go_base_url:
        opencode_go_base_url = _opencode_go_base_url(flat_env)
        for model in _VERIFIED_OPENCODE_GO_MODELS:
            if model.get("chat_compatible") is not True:
                continue
            model_id = str(model["id"])
            model_list.append(
                {
                    "model_name": f"opencode-go/{model_id}",
                    "litellm_params": {
                        "model": str(model["litellm_model"]),
                        "api_base": opencode_go_base_url,
                        "api_key": _env_ref(opencode_go_api_key_env),
                    },
                }
            )

    openrouter_api_key_env = _optional_env_name(upstream_creds, "openrouter_api_key_env")
    openrouter_model_metadata = upstream_creds.get("openrouter_model_metadata")
    for alias, target_model in _parse_openrouter_models(flat_env, "LITELLM_OPENROUTER_MODELS"):
        if openrouter_api_key_env is None:
            raise ValueError("Missing upstream OpenRouter env name for explicit alias routes")
        entry: dict[str, object] = {
            "model_name": alias,
            "litellm_params": {
                "model": _normalize_model_ref(target_model),
                "api_key": _env_ref(openrouter_api_key_env),
            },
        }
        model_info = _openrouter_model_info(target_model, openrouter_model_metadata)
        if model_info:
            entry["model_info"] = model_info
        model_list.append(entry)

    nvidia_api_key_env = _optional_env_name(upstream_creds, "nvidia_api_key_env")
    nvidia_base_url = _optional(flat_env, "NVIDIA_BASE_URL")
    for alias, target_model in _parse_alias_models(flat_env, "LITELLM_NVIDIA_MODELS"):
        if nvidia_base_url is None or nvidia_api_key_env is None:
            raise ValueError("NVIDIA routes require NVIDIA_BASE_URL and nvidia_api_key_env")
        model_list.append(
            {
                "model_name": alias,
                "litellm_params": {
                    "model": _normalize_model_ref(target_model),
                    "api_base": nvidia_base_url,
                    "api_key": _env_ref(nvidia_api_key_env),
                },
            }
        )

    return {"model_list": model_list, "litellm_settings": {"drop_params": True}}


def render_litellm_config_yaml(config: Mapping[str, object]) -> str:
    return _render_yaml_node(config).rstrip() + "\n"


def _opencode_go_base_url(flat_env: Mapping[str, str]) -> str:
    return (
        _optional(flat_env, "AI_DEFAULT_BASE_URL")
        or _optional(flat_env, "OPENCODE_GO_BASE_URL")
        or DEFAULT_OPENCODE_GO_BASE_URL
    )


def _local_alias(flat_env: Mapping[str, str]) -> str:
    provider = _optional(flat_env, "AI_DEFAULT_PROVIDER")
    model = _optional(flat_env, "AI_DEFAULT_MODEL")
    if provider is not None and model is not None:
        if provider == "local" or "." in provider:
            return f"{provider}/{model}"
    return DEFAULT_LOCAL_ALIAS


def _optional(flat_env: Mapping[str, str], key: str) -> str | None:
    value = flat_env.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_env_name(upstream_creds: Mapping[str, object], key: str) -> str:
    value = _optional_env_name(upstream_creds, key)
    if value is None:
        raise ValueError(f"Missing required upstream env name: {key}")
    return value


def _optional_env_name(upstream_creds: Mapping[str, object], key: str) -> str | None:
    value = upstream_creds.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _env_ref(env_name: str) -> str:
    return f"os.environ/{env_name}"


def _parse_alias_models(flat_env: Mapping[str, str], key: str) -> tuple[tuple[str, str], ...]:
    raw = _optional(flat_env, key)
    if raw is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in raw.split(","):
        alias, separator, target = item.partition("=")
        if separator != "=":
            raise ValueError(f"Expected alias=model format for {key}: {item}")
        normalized_alias = alias.strip()
        normalized_target = _normalize_model_ref(target.strip())
        if not normalized_alias or not normalized_target:
            raise ValueError(f"Expected non-empty alias=model format for {key}: {item}")
        pairs.append((normalized_alias, normalized_target))
    return tuple(pairs)


def _parse_openrouter_models(flat_env: Mapping[str, str], key: str) -> tuple[tuple[str, str], ...]:
    raw = _optional(flat_env, key)
    if raw is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in raw.split(","):
        normalized_item = item.strip()
        if not normalized_item:
            raise ValueError(f"Expected non-empty OpenRouter model entry for {key}: {item}")
        alias, separator, target = normalized_item.partition("=")
        if separator == "=":
            normalized_alias = alias.strip()
            normalized_target = _normalize_openrouter_target(target.strip())
        else:
            normalized_target = _normalize_openrouter_target(normalized_item)
            normalized_alias = normalized_target
        _validate_openrouter_route(normalized_alias)
        _validate_openrouter_route(normalized_target)
        pairs.append((normalized_alias, normalized_target))
    return tuple(pairs)


def _validate_openrouter_route(model_ref: str) -> None:
    if model_ref in {"*", "openrouter/*"}:
        raise ValueError("OpenRouter wildcard routes are not allowed")


def _normalize_openrouter_target(model_ref: str) -> str:
    normalized = _normalize_model_ref(model_ref)
    if normalized in {"*", "openrouter/*"}:
        return normalized
    if normalized.startswith("openrouter/"):
        return normalized
    return f"openrouter/{normalized}"


def _openrouter_model_info(
    target_model: str, metadata: object
) -> dict[str, float]:
    if not isinstance(metadata, Mapping):
        return {}
    candidate_keys = (target_model, target_model.removeprefix("openrouter/"))
    raw_entry: object = None
    for candidate in candidate_keys:
        raw_entry = metadata.get(candidate)
        if isinstance(raw_entry, Mapping):
            break
    if not isinstance(raw_entry, Mapping):
        return {}
    pricing = raw_entry.get("pricing")
    if not isinstance(pricing, Mapping):
        return {}
    model_info: dict[str, float] = {}
    input_cost = _coerce_float(pricing.get("prompt"))
    if input_cost is not None:
        model_info["input_cost_per_token"] = input_cost
    output_cost = _coerce_float(pricing.get("completion"))
    if output_cost is not None:
        model_info["output_cost_per_token"] = output_cost
    return model_info


def _coerce_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return float(stripped)


def _normalize_model_ref(model_ref: str) -> str:
    legacy_aliases = {
        "nvidia/moonshot/kimi-k2.5": "nvidia/moonshotai/kimi-k2.5",
    }
    return legacy_aliases.get(model_ref, model_ref)


def _normalize_local_model_ref(model_ref: str) -> str:
    normalized = _normalize_model_ref(model_ref)
    if normalized.startswith("openai/"):
        _, _, remainder = normalized.partition("openai/")
        upstream_provider, separator, _ = remainder.partition("/")
        if separator == "/" and "." in upstream_provider:
            raise ValueError(
                "Unexpected mangled local upstream target: "
                f"{normalized}. Use the bare local model id instead."
            )
    if "/" not in normalized:
        return f"openai/{normalized}"
    provider, _, _ = normalized.partition("/")
    if provider in {
        "openai",
        "azure",
        "anthropic",
        "bedrock",
        "vertex_ai",
        "gemini",
        "nvidia",
        "huggingface",
        "ollama",
        "openrouter",
    }:
        return normalized
    if "." in provider:
        raise ValueError(
            "Unexpected hostname local upstream target: "
            f"{normalized}. Use the bare local model id instead."
        )
    return f"openai/{normalized}"


def _render_yaml_node(value: object, *, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, child in value.items():
            if isinstance(child, Mapping | list):
                lines.append(f"{prefix}{key}:")
                lines.append(_render_yaml_node(child, indent=indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_render_yaml_scalar(child)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for child in value:
            if isinstance(child, Mapping):
                nested = _render_yaml_node(child, indent=indent + 2).splitlines()
                lines.append(f"{prefix}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            elif isinstance(child, list):
                lines.append(f"{prefix}-")
                lines.append(_render_yaml_node(child, indent=indent + 2))
            else:
                lines.append(f"{prefix}- {_render_yaml_scalar(child)}")
        return "\n".join(lines)
    return f"{prefix}{_render_yaml_scalar(value)}"


def _render_yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace('"', '\\"')
    return f'"{text}"'
