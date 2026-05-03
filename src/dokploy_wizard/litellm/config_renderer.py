from __future__ import annotations

from collections.abc import Mapping, Sequence

DEFAULT_LOCAL_ALIAS = "local/unsloth-active"
DEFAULT_LOCAL_MODEL = "unsloth/Qwen2.5-Coder-32B-Instruct"
DEFAULT_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"


def build_litellm_config(
    flat_env: Mapping[str, str], upstream_creds: Mapping[str, object]
) -> dict[str, object]:
    model_list: list[dict[str, object]] = []

    local_base_url = _optional(flat_env, "LITELLM_LOCAL_BASE_URL")
    if local_base_url is not None:
        model_list.append(
            {
                "model_name": DEFAULT_LOCAL_ALIAS,
                "litellm_params": {
                    "model": _optional(flat_env, "LITELLM_LOCAL_MODEL") or DEFAULT_LOCAL_MODEL,
                    "api_base": local_base_url,
                    "api_key": "none",
                },
            }
        )

    opencode_go_api_key_env = _required_env_name(upstream_creds, "opencode_go_api_key_env")
    model_list.append(
        {
            "model_name": "openai/*",
            "litellm_params": {
                "model": "openai/*",
                "api_base": _opencode_go_base_url(flat_env),
                "api_key": _env_ref(opencode_go_api_key_env),
            },
        }
    )

    openrouter_api_key_env = _optional_env_name(upstream_creds, "openrouter_api_key_env")
    for alias, target_model in _parse_alias_models(flat_env, "LITELLM_OPENROUTER_MODELS"):
        if alias in {"openrouter/*", "*"} or target_model in {"openrouter/*", "*"}:
            raise ValueError("OpenRouter wildcard routes are not allowed")
        if openrouter_api_key_env is None:
            raise ValueError("Missing upstream OpenRouter env name for explicit alias routes")
        model_list.append(
            {
                "model_name": alias,
                "litellm_params": {
                    "model": _normalize_model_ref(target_model),
                    "api_key": _env_ref(openrouter_api_key_env),
                },
            }
        )

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

    return {"model_list": model_list}


def render_litellm_config_yaml(config: Mapping[str, object]) -> str:
    return _render_yaml_node(config).rstrip() + "\n"


def _opencode_go_base_url(flat_env: Mapping[str, str]) -> str:
    return (
        _optional(flat_env, "AI_DEFAULT_BASE_URL")
        or _optional(flat_env, "OPENCODE_GO_BASE_URL")
        or DEFAULT_OPENCODE_GO_BASE_URL
    )


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


def _normalize_model_ref(model_ref: str) -> str:
    legacy_aliases = {
        "nvidia/moonshot/kimi-k2.5": "nvidia/moonshotai/kimi-k2.5",
    }
    return legacy_aliases.get(model_ref, model_ref)


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
