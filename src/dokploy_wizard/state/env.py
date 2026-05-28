# ruff: noqa: E501
"""Env-file parsing and desired-state resolution."""

from __future__ import annotations

import re
from pathlib import Path
from urllib import parse

from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.state.models import DesiredState, RawEnvInput, StateValidationError

_ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_OPENCLAW_NEXA_ENV_KEYS = {
    "OPENCLAW_NEXA_DEPLOYMENT_MODE",
    "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL",
    "OPENCLAW_NEXA_TALK_SHARED_SECRET",
    "OPENCLAW_NEXA_TALK_SIGNING_SECRET",
    "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET",
    "OPENCLAW_NEXA_EDITOR_EVENTS_SHARED_SECRET",
    "OPENCLAW_NEXA_WEBDAV_AUTH_USER",
    "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD",
    "OPENCLAW_NEXA_AGENT_USER_ID",
    "OPENCLAW_NEXA_AGENT_DISPLAY_NAME",
    "OPENCLAW_NEXA_AGENT_PASSWORD",
    "OPENCLAW_NEXA_AGENT_EMAIL",
    "OPENCLAW_NEXA_MEM0_BASE_URL",
    "OPENCLAW_NEXA_MEM0_API_KEY",
    "OPENCLAW_NEXA_MEM0_LLM_BASE_URL",
    "OPENCLAW_NEXA_MEM0_LLM_API_KEY",
    "OPENCLAW_NEXA_MEM0_EMBEDDER_MODEL",
    "OPENCLAW_NEXA_MEM0_EMBEDDER_DIMENSIONS",
    "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND",
    "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL",
    "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY",
    "OPENCLAW_NEXA_MEM0_VECTOR_DIMENSIONS",
    "OPENCLAW_NEXA_PRESENCE_POLICY",
}
_MY_FARM_ADVISOR_DIRECT_PROVIDER_ENV_KEYS = {
    "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
    "MY_FARM_ADVISOR_NVIDIA_API_KEY",
}
_MY_FARM_ADVISOR_SHARED_PROVIDER_ENV_KEYS = {
    "AI_DEFAULT_API_KEY",
    "AI_DEFAULT_BASE_URL",
    "AI_DEFAULT_PROVIDER",
    "AI_DEFAULT_MODEL",
    "ANTHROPIC_API_KEY",
}
_LITELLM_CANONICAL_ENV_KEYS = {
    "AI_DEFAULT_PROVIDER",
    "AI_DEFAULT_MODEL",
    "LITELLM_IMAGE",
    "LITELLM_IMAGE_TAG",
    "LITELLM_VERSION",
    "LITELLM_ADMIN_SUBDOMAIN",
    "LITELLM_LOCAL_BASE_URL",
    "LITELLM_LOCAL_MODEL",
    "LITELLM_LOCAL_API_KEY",
    "LITELLM_OPENROUTER_API_KEY",
    "LITELLM_OPENROUTER_MODELS",
    "LITELLM_OPENCODE_GO_API_KEY",
    "LITELLM_NVIDIA_API_KEY",
    "LITELLM_NVIDIA_BASE_URL",
    "LITELLM_NVIDIA_MODELS",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_GO_BASE_URL",
    "OPENROUTER_API_KEY",
    "NVIDIA_BASE_URL",
}
_LITELLM_LOCAL_ENV_KEYS = {
    "LITELLM_LOCAL_BASE_URL",
    "LITELLM_LOCAL_MODEL",
    "LITELLM_LOCAL_API_KEY",
}
_MY_FARM_ADVISOR_OPTIONAL_ENV_KEYS = {
    "MY_FARM_ADVISOR_PRIMARY_MODEL",
    "MY_FARM_ADVISOR_FALLBACK_MODELS",
    "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
    "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID",
}
_MY_FARM_ADVISOR_FEATURE_GATED_ENV_KEYS = {
    "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
    "TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE",
    "TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS",
    "TELEGRAM_DATA_PIPELINE_BOT_TOKEN",
    "TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE",
    "TELEGRAM_DATA_PIPELINE_ALLOWED_USERS",
    "TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS",
    "TELEGRAM_ALLOWED_USERS",
    "R2_BUCKET_NAME",
    "R2_ENDPOINT",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "CF_ACCOUNT_ID",
    "DATA_MODE",
    "WORKSPACE_DATA_R2_RCLONE_MOUNT",
    "WORKSPACE_DATA_R2_PREFIX",
}
_MY_FARM_ADVISOR_PACK_ONLY_ENV_KEYS = (
    _MY_FARM_ADVISOR_OPTIONAL_ENV_KEYS | _MY_FARM_ADVISOR_FEATURE_GATED_ENV_KEYS
)


def parse_env_file(path: Path) -> RawEnvInput:
    values: dict[str, str] = {}

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            msg = f"Invalid env line {line_number}: expected KEY=VALUE."
            raise StateValidationError(msg)
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _ENV_KEY_PATTERN.fullmatch(key):
            msg = f"Invalid env key '{key}' on line {line_number}."
            raise StateValidationError(msg)
        if key in values:
            msg = f"Duplicate env key '{key}' on line {line_number}."
            raise StateValidationError(msg)
        values[key] = value

    return RawEnvInput(format_version=1, values=values)


def resolve_desired_state(raw_env: RawEnvInput) -> DesiredState:
    values = raw_env.values
    root_domain = _require_value(values, "ROOT_DOMAIN")
    stack_name = _get_configured_value(values, "STACK_NAME") or derive_stack_name_from_root_domain(
        root_domain
    )
    resolve_ai_default_model_ref(values)
    parse_litellm_openrouter_models(values)
    parse_litellm_nvidia_models(values)
    _validate_litellm_local_env(values)
    _validate_litellm_nvidia_env(values)
    dokploy_subdomain = _get_configured_value(values, "DOKPLOY_SUBDOMAIN") or "dokploy"
    hostnames: dict[str, str] = {
        "dokploy": _join_hostname(dokploy_subdomain, root_domain),
    }
    from dokploy_wizard.packs.resolver import resolve_pack_selection

    pack_selection = resolve_pack_selection(values, root_domain=root_domain)
    _validate_openclaw_nexa_env(values, enabled_packs=pack_selection.enabled_packs)
    _validate_my_farm_advisor_env(values, enabled_packs=pack_selection.enabled_packs)
    hostnames.update(pack_selection.hostnames)

    return DesiredState(
        format_version=1,
        stack_name=stack_name,
        root_domain=root_domain,
        dokploy_url=f"https://{hostnames['dokploy']}",
        dokploy_api_url=_resolve_dokploy_api_url(values),
        enable_tailscale=_resolve_tailscale_enabled(values),
        tailscale_hostname=_resolve_tailscale_hostname(values),
        tailscale_enable_ssh=_resolve_tailscale_enable_ssh(values),
        tailscale_tags=_resolve_tailscale_csv(values, key="TAILSCALE_TAGS"),
        tailscale_subnet_routes=_resolve_tailscale_csv(values, key="TAILSCALE_SUBNET_ROUTES"),
        cloudflare_access_otp_emails=_resolve_access_otp_emails(
            values, pack_selection.enabled_packs
        ),
        enabled_features=pack_selection.enabled_features,
        selected_packs=pack_selection.selected_packs,
        enabled_packs=pack_selection.enabled_packs,
        hostnames=dict(sorted(hostnames.items())),
        seaweedfs_access_key=_resolve_seaweedfs_secret(
            values, enabled_packs=pack_selection.enabled_packs, key="SEAWEEDFS_ACCESS_KEY"
        ),
        seaweedfs_secret_key=_resolve_seaweedfs_secret(
            values, enabled_packs=pack_selection.enabled_packs, key="SEAWEEDFS_SECRET_KEY"
        ),
        openclaw_gateway_token=_resolve_openclaw_gateway_token(
            values, enabled_packs=pack_selection.enabled_packs
        ),
        openclaw_channels=pack_selection.openclaw_channels,
        openclaw_replicas=_resolve_openclaw_replicas(values, pack_selection.enabled_packs),
        my_farm_advisor_channels=pack_selection.my_farm_advisor_channels,
        my_farm_advisor_replicas=_resolve_pack_replicas(
            values,
            pack_selection.enabled_packs,
            key="MY_FARM_ADVISOR_REPLICAS",
            pack_name="my-farm-advisor",
        ),
        shared_core=build_shared_core_plan(stack_name, pack_selection.enabled_packs, values),
    )


def _join_hostname(subdomain: str, root_domain: str) -> str:
    return f"{subdomain}.{root_domain}".lower()


def derive_stack_name_from_root_domain(root_domain: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", root_domain.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or "dokploy-stack"


def _require_value(values: dict[str, str], key: str) -> str:
    value = _get_configured_value(values, key)
    if value is None:
        msg = f"Missing required env key '{key}'."
        raise StateValidationError(msg)
    return value


def _get_configured_value(values: dict[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized


def _has_configured_value(values: dict[str, str], key: str) -> bool:
    return _get_configured_value(values, key) is not None


def resolve_ai_default_model_ref(values: dict[str, str]) -> str | None:
    key_provider = "AI_DEFAULT_PROVIDER"
    key_model = "AI_DEFAULT_MODEL"
    if key_provider in values or key_model in values:
        provider = _get_configured_value(values, key_provider)
        model = _get_configured_value(values, key_model)
        if provider is None and model is None:
            return None
        if provider is None:
            raise StateValidationError(
                "AI_DEFAULT_PROVIDER is required when AI_DEFAULT_MODEL is set. Empty strings count as unset."
            )
        if model is None:
            raise StateValidationError(
                "AI_DEFAULT_MODEL is required when AI_DEFAULT_PROVIDER is set. Empty strings count as unset."
            )
        canonical_provider = _canonical_ai_default_provider(provider)
        if model.startswith(f"{canonical_provider}/"):
            return model
        return f"{canonical_provider}/{model}"

    legacy_provider = _get_configured_value(values, "ADVISOR_MODEL_PROVIDER")
    legacy_model = _get_configured_value(values, "ADVISOR_MODEL_NAME")
    if legacy_provider is None and legacy_model is None:
        return None
    if legacy_provider is None or legacy_model is None:
        return None
    if legacy_model.startswith(f"{legacy_provider}/"):
        return legacy_model
    return f"{legacy_provider}/{legacy_model}"


def parse_litellm_openrouter_models(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    raw_value = _get_configured_value(values, "LITELLM_OPENROUTER_MODELS")
    if raw_value is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in raw_value.split(","):
        normalized_item = item.strip()
        if normalized_item == "":
            continue
        alias, separator, target_model = normalized_item.partition("=")
        if separator == "":
            alias = normalized_item
            target = normalized_item
        else:
            alias = alias.strip()
            target = target_model.strip()
        if alias == "" or target == "":
            raise StateValidationError(
                "LITELLM_OPENROUTER_MODELS entries must be raw model IDs or alias=model pairs."
            )
        if alias in {"openrouter/*", "*"} or target in {"openrouter/*", "*"}:
            raise StateValidationError(
                "LITELLM_OPENROUTER_MODELS does not allow wildcard OpenRouter routes."
            )
        pairs.append((alias, target))
    return tuple(pairs)


def parse_litellm_nvidia_models(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    raw_value = _get_configured_value(values, "LITELLM_NVIDIA_MODELS")
    if raw_value is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in raw_value.split(","):
        normalized_item = item.strip()
        if normalized_item == "":
            continue
        alias, separator, target_model = normalized_item.partition("=")
        if separator == "=":
            alias = _normalize_litellm_nvidia_ref(alias.strip())
            target = _normalize_litellm_nvidia_ref(target_model.strip())
        else:
            alias = _normalize_litellm_nvidia_ref(normalized_item)
            target = alias
        if alias == "" or target == "":
            raise StateValidationError(
                "LITELLM_NVIDIA_MODELS entries must be raw model IDs or non-empty "
                "alias=model pairs."
            )
        pairs.append((alias, target))
    return tuple(pairs)


def _normalize_litellm_nvidia_ref(model_ref: str) -> str:
    if model_ref == "" or model_ref.startswith("nvidia/"):
        return model_ref
    if model_ref == "nvidia/moonshot/kimi-k2.5":
        return "nvidia/moonshotai/kimi-k2.5"
    return f"nvidia/{model_ref}"


def _litellm_nvidia_base_url(values: dict[str, str]) -> str | None:
    return _get_configured_value(values, "LITELLM_NVIDIA_BASE_URL") or _get_configured_value(
        values, "NVIDIA_BASE_URL"
    )


def _validate_litellm_nvidia_env(values: dict[str, str]) -> None:
    configured_keys = {
        key
        for key in ("LITELLM_NVIDIA_API_KEY", "LITELLM_NVIDIA_MODELS")
        if _has_configured_value(values, key)
    }
    default_provider = _get_configured_value(values, "AI_DEFAULT_PROVIDER")
    default_selects_nvidia = (
        default_provider is not None and _canonical_ai_default_provider(default_provider) == "nvidia"
    )
    if not configured_keys and not default_selects_nvidia:
        return
    missing = sorted(
        key
        for key in ("LITELLM_NVIDIA_API_KEY", "LITELLM_NVIDIA_BASE_URL", "LITELLM_NVIDIA_MODELS")
        if not _has_configured_value(values, key)
    )
    if "LITELLM_NVIDIA_BASE_URL" in missing and _has_configured_value(values, "NVIDIA_BASE_URL"):
        missing.remove("LITELLM_NVIDIA_BASE_URL")
    if missing:
        joined = ", ".join(missing)
        raise StateValidationError(
            "NVIDIA LiteLLM routing requires LITELLM_NVIDIA_API_KEY, "
            "LITELLM_NVIDIA_BASE_URL, and LITELLM_NVIDIA_MODELS raw model IDs or "
            f"alias=model pairs. Missing: {joined}."
        )


def _validate_litellm_local_env(values: dict[str, str]) -> None:
    if _has_configured_value(values, "LITELLM_LOCAL_BASE_URL"):
        missing_local_keys = sorted(
            key
            for key in ("LITELLM_LOCAL_MODEL", "LITELLM_LOCAL_API_KEY")
            if not _has_configured_value(values, key)
        )
        if missing_local_keys:
            joined = ", ".join(missing_local_keys)
            raise StateValidationError(
                "Local LiteLLM routing requires LITELLM_LOCAL_BASE_URL, "
                f"LITELLM_LOCAL_MODEL, and LITELLM_LOCAL_API_KEY. Missing: {joined}."
            )
        return
    provider = _get_configured_value(values, "AI_DEFAULT_PROVIDER")
    default_selects_local = provider is not None and _ai_default_provider_is_local(provider)
    dangling_local_keys = sorted(
        key
        for key in ("LITELLM_LOCAL_MODEL", "LITELLM_LOCAL_API_KEY")
        if _has_configured_value(values, key)
    )
    if default_selects_local or dangling_local_keys:
        raise StateValidationError(
            "Local LiteLLM routing requires LITELLM_LOCAL_BASE_URL. Leave local model keys "
            "commented to use OpenRouter or another remote provider by default."
        )


def _ai_default_provider_is_local(provider: str) -> bool:
    normalized = _canonical_ai_default_provider(provider)
    return normalized == "local" or "." in normalized


def _canonical_ai_default_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    aliases = {"opencode": "opencode-go"}
    return aliases.get(normalized, normalized)


def _litellm_nvidia_configured(values: dict[str, str]) -> bool:
    return (
        _has_configured_value(values, "LITELLM_NVIDIA_API_KEY")
        and _litellm_nvidia_base_url(values) is not None
        and bool(parse_litellm_nvidia_models(values))
    )


def _resolve_dokploy_api_url(values: dict[str, str]) -> str | None:
    raw_url = _get_configured_value(values, "DOKPLOY_API_URL")
    raw_key = _get_configured_value(values, "DOKPLOY_API_KEY")
    if raw_url is None and raw_key is None:
        return None
    if raw_key is None:
        raise StateValidationError("DOKPLOY_API_URL and DOKPLOY_API_KEY must be provided together.")
    if raw_url is None:
        return "https://" + _join_hostname(
            _get_configured_value(values, "DOKPLOY_SUBDOMAIN") or "dokploy",
            _require_value(values, "ROOT_DOMAIN"),
        )
    parsed = parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc == "":
        raise StateValidationError(
            f"DOKPLOY_API_URL must be an absolute http(s) URL, found {raw_url!r}."
        )
    return raw_url.rstrip("/")


def _resolve_tailscale_enabled(values: dict[str, str]) -> bool:
    raw_value = _get_configured_value(values, "ENABLE_TAILSCALE")
    enabled = False if raw_value is None else _parse_bool(raw_value, key="ENABLE_TAILSCALE")
    _validate_tailscale_env(enabled=enabled, values=values)
    return enabled


def _resolve_tailscale_hostname(values: dict[str, str]) -> str | None:
    if not _resolve_tailscale_enabled(values):
        return None


    return _require_value(values, "TAILSCALE_HOSTNAME")


def _resolve_tailscale_enable_ssh(values: dict[str, str]) -> bool:
    if not _resolve_tailscale_enabled(values):
        return False
    raw_value = _get_configured_value(values, "TAILSCALE_ENABLE_SSH")
    if raw_value is None:
        return False
    return _parse_bool(raw_value, key="TAILSCALE_ENABLE_SSH")


def _resolve_tailscale_csv(values: dict[str, str], *, key: str) -> tuple[str, ...]:
    if not _resolve_tailscale_enabled(values):
        return ()
    raw_value = _get_configured_value(values, key) or ""
    if raw_value == "":
        return ()
    items = tuple(sorted({item.strip() for item in raw_value.split(",") if item.strip()}))
    if key == "TAILSCALE_TAGS":
        invalid = [item for item in items if not item.startswith("tag:")]
        if invalid:
            raise StateValidationError(
                f"TAILSCALE_TAGS entries must start with 'tag:', found {invalid}."
            )
    if key == "TAILSCALE_SUBNET_ROUTES":
        invalid = [item for item in items if "/" not in item]
        if invalid:
            raise StateValidationError(
                f"TAILSCALE_SUBNET_ROUTES entries must be CIDR routes, found {invalid}."
            )
    return items


def _validate_tailscale_env(*, enabled: bool, values: dict[str, str]) -> None:
    tailscale_keys = {
        "TAILSCALE_AUTH_KEY",
        "TAILSCALE_HOSTNAME",
        "TAILSCALE_ENABLE_SSH",
        "TAILSCALE_TAGS",
        "TAILSCALE_SUBNET_ROUTES",
    }
    if enabled:
        _require_value(values, "TAILSCALE_AUTH_KEY")
        _require_value(values, "TAILSCALE_HOSTNAME")
        return
    unexpected = sorted(key for key in tailscale_keys if key in values)
    if unexpected:
        raise StateValidationError(f"{unexpected} require ENABLE_TAILSCALE=true.")


def _parse_bool(raw_value: str, *, key: str) -> bool:
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise StateValidationError(f"Invalid boolean value for '{key}': {raw_value!r}.")


def _resolve_access_otp_emails(
    values: dict[str, str], enabled_packs: tuple[str, ...]
) -> tuple[str, ...]:
    raw_value = _get_configured_value(values, "CLOUDFLARE_ACCESS_OTP_EMAILS") or ""
    if raw_value == "":
        if {"openclaw", "my-farm-advisor"} & set(enabled_packs):
            admin_email = values.get("DOKPLOY_ADMIN_EMAIL", "").strip().lower()
            if admin_email != "" and "@" in admin_email:
                return (admin_email,)
        return ()
    if not ({"openclaw", "my-farm-advisor"} & set(enabled_packs)):
        raise StateValidationError(
            "CLOUDFLARE_ACCESS_OTP_EMAILS requires the openclaw or my-farm-advisor pack."
        )
    items = tuple(sorted({item.strip().lower() for item in raw_value.split(",") if item.strip()}))
    invalid = [item for item in items if "@" not in item]
    if invalid:
        raise StateValidationError(
            f"CLOUDFLARE_ACCESS_OTP_EMAILS entries must be valid email addresses, found {invalid}."
        )
    return items


def _resolve_seaweedfs_secret(
    values: dict[str, str], *, enabled_packs: tuple[str, ...], key: str
) -> str | None:
    raw_value = _get_configured_value(values, key)
    if "seaweedfs" not in enabled_packs:
        if raw_value is not None:
            raise StateValidationError(f"{key} requires the 'seaweedfs' pack.")
        return None
    return raw_value


def _resolve_openclaw_replicas(
    values: dict[str, str], enabled_packs: tuple[str, ...]
) -> int | None:
    return _resolve_pack_replicas(
        values,
        enabled_packs,
        key="OPENCLAW_REPLICAS",
        pack_name="openclaw",
    )


def _resolve_pack_replicas(
    values: dict[str, str], enabled_packs: tuple[str, ...], *, key: str, pack_name: str
) -> int | None:
    raw_value = _get_configured_value(values, key)
    if pack_name not in enabled_packs:
        if raw_value is not None:
            raise StateValidationError(f"{key} requires the '{pack_name}' pack.")
        return None
    if raw_value is None:
        return 1
    try:
        parsed = int(raw_value)
    except ValueError as error:
        raise StateValidationError(
            f"OPENCLAW_REPLICAS must be a positive integer, found {raw_value!r}."
        ) from error
    if parsed < 1:
        raise StateValidationError(
            f"OPENCLAW_REPLICAS must be a positive integer, found {raw_value!r}."
        )
    return parsed


def _resolve_openclaw_gateway_token(
    values: dict[str, str], *, enabled_packs: tuple[str, ...]
) -> str | None:
    raw_value = _get_configured_value(values, "OPENCLAW_GATEWAY_TOKEN")
    if "openclaw" not in enabled_packs:
        if raw_value is not None:
            raise StateValidationError("OPENCLAW_GATEWAY_TOKEN requires the 'openclaw' pack.")
        return None
    return raw_value


def _validate_openclaw_nexa_env(
    values: dict[str, str], *, enabled_packs: tuple[str, ...]
) -> None:
    if "openclaw" in enabled_packs:
        return
    unexpected = sorted(key for key in _OPENCLAW_NEXA_ENV_KEYS if _has_configured_value(values, key))
    if unexpected:
        raise StateValidationError(f"{unexpected} require the 'openclaw' pack.")


def _validate_my_farm_advisor_env(
    values: dict[str, str], *, enabled_packs: tuple[str, ...]
) -> None:
    if "my-farm-advisor" not in enabled_packs:
        unexpected = sorted(
            key for key in _MY_FARM_ADVISOR_PACK_ONLY_ENV_KEYS if _has_configured_value(values, key)
        )
        if unexpected:
            raise StateValidationError(f"{unexpected} require the 'my-farm-advisor' pack.")
        return

    if any(_has_configured_value(values, key) for key in _MY_FARM_ADVISOR_DIRECT_PROVIDER_ENV_KEYS):
        return
    if _has_configured_value(values, "ANTHROPIC_API_KEY"):
        return

    has_shared_api_key = _has_configured_value(values, "AI_DEFAULT_API_KEY")
    has_shared_base_url = _has_configured_value(values, "AI_DEFAULT_BASE_URL")
    if has_shared_api_key and has_shared_base_url:
        return
    if has_shared_api_key or has_shared_base_url:
        raise StateValidationError(
            "My Farm Advisor shared provider fallback requires both AI_DEFAULT_API_KEY and "
            "AI_DEFAULT_BASE_URL. Empty strings count as unset."
        )

    ai_default_model_ref = resolve_ai_default_model_ref(values)
    if ai_default_model_ref is not None:
        if not ai_default_model_ref.startswith("nvidia/"):
            return
        if _litellm_nvidia_configured(values):
            return

    has_legacy_opencode_go_api_key = _has_configured_value(values, "OPENCODE_GO_API_KEY")
    has_legacy_opencode_go_base_url = _has_configured_value(values, "OPENCODE_GO_BASE_URL")
    if has_legacy_opencode_go_api_key and has_legacy_opencode_go_base_url:
        return
    if has_legacy_opencode_go_api_key or has_legacy_opencode_go_base_url:
        raise StateValidationError(
            "LiteLLM OpenCode Go compatibility requires both OPENCODE_GO_API_KEY and "
            "OPENCODE_GO_BASE_URL. Empty strings count as unset."
        )

    openrouter_models = parse_litellm_openrouter_models(values)
    has_litellm_openrouter_api_key = _has_configured_value(values, "LITELLM_OPENROUTER_API_KEY")
    if has_litellm_openrouter_api_key and openrouter_models:
        return
    if has_litellm_openrouter_api_key and not openrouter_models:
        raise StateValidationError(
            "LiteLLM OpenRouter routing for My Farm Advisor requires both "
            "LITELLM_OPENROUTER_API_KEY and LITELLM_OPENROUTER_MODELS. Empty strings count "
            "as unset."
        )

    if _has_configured_value(values, "LITELLM_OPENCODE_GO_API_KEY"):
        return

    if _litellm_nvidia_configured(values):
        return

    if _has_configured_value(values, "LITELLM_LOCAL_BASE_URL"):
        return
    if _has_configured_value(values, "LITELLM_LOCAL_MODEL") or any(
        _has_configured_value(values, key) for key in _LITELLM_LOCAL_ENV_KEYS
    ):
        raise StateValidationError(
            "LiteLLM canonical local mode for My Farm Advisor requires LITELLM_LOCAL_BASE_URL. "
            "Point it at the reachable Tailnet/local vLLM endpoint. Empty strings count as unset."
        )

    raise StateValidationError(
        "My Farm Advisor requires at least one provider configuration when enabled. Set "
        "MY_FARM_ADVISOR_OPENROUTER_API_KEY, MY_FARM_ADVISOR_NVIDIA_API_KEY, "
        "ANTHROPIC_API_KEY, both AI_DEFAULT_API_KEY and AI_DEFAULT_BASE_URL, both "
        "AI_DEFAULT_PROVIDER and AI_DEFAULT_MODEL, both OPENCODE_GO_API_KEY and "
        "OPENCODE_GO_BASE_URL, LITELLM_OPENROUTER_API_KEY with LITELLM_OPENROUTER_MODELS, "
        "LITELLM_OPENCODE_GO_API_KEY, LITELLM_NVIDIA_API_KEY with "
        "LITELLM_NVIDIA_BASE_URL and LITELLM_NVIDIA_MODELS, or LITELLM_LOCAL_BASE_URL "
        "for LiteLLM canonical local "
        "mode. Empty strings count as unset."
    )
