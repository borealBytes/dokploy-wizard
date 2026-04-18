"""Minimal CLI helpers for guided pack selection."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from dokploy_wizard.packs.catalog import get_pack_definition
from dokploy_wizard.state.models import RawEnvInput, StateValidationError

PromptFn = Callable[[str], str]

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
_CARET_CSI_RE = re.compile(r"\^\[\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class PromptSelection:
    selected_packs: tuple[str, ...]
    disabled_packs: tuple[str, ...]
    seaweedfs_access_key: str | None
    seaweedfs_secret_key: str | None
    generated_secrets: dict[str, str]
    advisor_env: dict[str, str]
    openclaw_channels: tuple[str, ...]
    my_farm_advisor_channels: tuple[str, ...]


_ADVISOR_ENV_KEYS = (
    "OPENCLAW_OPENROUTER_API_KEY",
    "OPENCLAW_NVIDIA_API_KEY",
    "OPENCLAW_PRIMARY_MODEL",
    "OPENCLAW_FALLBACK_MODELS",
    "OPENCLAW_TELEGRAM_BOT_TOKEN",
    "OPENCLAW_TELEGRAM_OWNER_USER_ID",
    "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
    "MY_FARM_ADVISOR_NVIDIA_API_KEY",
    "MY_FARM_ADVISOR_PRIMARY_MODEL",
    "MY_FARM_ADVISOR_FALLBACK_MODELS",
    "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
    "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID",
)
_DEFAULT_NVIDIA_PRIMARY_MODEL = "nvidia/moonshotai/kimi-k2.5"
_DEFAULT_OPENROUTER_FALLBACK_MODEL = "openrouter/openrouter/free"
_DEFAULT_DOKPLOY_ADMIN_EMAIL = "clayton@superiorbyteworks.com"
_OPENCLAW_ADVISOR_ENV_KEYS = tuple(key for key in _ADVISOR_ENV_KEYS if key.startswith("OPENCLAW_"))
_MY_FARM_ADVISOR_ENV_KEYS = tuple(
    key for key in _ADVISOR_ENV_KEYS if key.startswith("MY_FARM_ADVISOR_")
)


@dataclass(frozen=True)
class GuidedInstallValues:
    stack_name: str
    root_domain: str
    dokploy_subdomain: str
    dokploy_admin_email: str
    dokploy_admin_password: str | None
    enable_headscale: bool
    cloudflare_api_token: str
    cloudflare_account_id: str
    cloudflare_zone_id: str | None
    enable_tailscale: bool
    tailscale_auth_key: str | None
    tailscale_hostname: str | None
    tailscale_enable_ssh: bool
    tailscale_tags: tuple[str, ...]
    tailscale_subnet_routes: tuple[str, ...]


def apply_prompt_selection(raw_env: RawEnvInput, selection: PromptSelection) -> RawEnvInput:
    updated_values = dict(raw_env.values)
    if selection.selected_packs:
        updated_values["PACKS"] = ",".join(selection.selected_packs)
    else:
        updated_values.pop("PACKS", None)
    for pack_name in selection.disabled_packs:
        updated_values[get_pack_definition(pack_name).env_flag] = "false"
    if selection.seaweedfs_access_key is not None:
        updated_values["SEAWEEDFS_ACCESS_KEY"] = selection.seaweedfs_access_key
    else:
        updated_values.pop("SEAWEEDFS_ACCESS_KEY", None)
    if selection.seaweedfs_secret_key is not None:
        updated_values["SEAWEEDFS_SECRET_KEY"] = selection.seaweedfs_secret_key
    else:
        updated_values.pop("SEAWEEDFS_SECRET_KEY", None)
    if selection.openclaw_channels:
        updated_values["OPENCLAW_CHANNELS"] = ",".join(selection.openclaw_channels)
    elif "openclaw" in selection.disabled_packs:
        updated_values.pop("OPENCLAW_CHANNELS", None)
    if selection.my_farm_advisor_channels:
        updated_values["MY_FARM_ADVISOR_CHANNELS"] = ",".join(selection.my_farm_advisor_channels)
    elif "my-farm-advisor" in selection.disabled_packs:
        updated_values.pop("MY_FARM_ADVISOR_CHANNELS", None)
    if "openclaw" in selection.disabled_packs:
        for key in _OPENCLAW_ADVISOR_ENV_KEYS:
            updated_values.pop(key, None)
        updated_values.pop("OPENCLAW_GATEWAY_TOKEN", None)
    if "my-farm-advisor" in selection.disabled_packs:
        for key in _MY_FARM_ADVISOR_ENV_KEYS:
            updated_values.pop(key, None)
    updated_values.update(selection.advisor_env)
    return RawEnvInput(format_version=raw_env.format_version, values=updated_values)


def prompt_for_pack_selection(
    prompt: PromptFn = input,
    *,
    include_headscale_prompt: bool = True,
    headscale_default: bool = True,
) -> PromptSelection:
    selected: list[str] = []
    disabled: list[str] = []
    if include_headscale_prompt:
        if _prompt_yes_no(prompt, "Enable Headscale? [Y/n]: ", default=headscale_default):
            selected.append("headscale")
        else:
            disabled.append("headscale")
    if _prompt_yes_no(prompt, "Enable Matrix? [y/N]: ", default=False):
        selected.append("matrix")
    if _prompt_yes_no(prompt, "Enable Nextcloud + OnlyOffice? [Y/n]: ", default=True):
        selected.append("nextcloud")
    seaweedfs_access_key: str | None = None
    seaweedfs_secret_key: str | None = None
    generated_secrets: dict[str, str] = {}
    advisor_env: dict[str, str] = {}
    if _prompt_yes_no(prompt, "Enable SeaweedFS object storage? [Y/n]: ", default=True):
        selected.append("seaweedfs")
        seaweedfs_access_key = _generate_credential(prefix="seaweed")
        seaweedfs_secret_key = _generate_credential(prefix="seaweed-secret")
        generated_secrets["SEAWEEDFS_ACCESS_KEY"] = seaweedfs_access_key
        generated_secrets["SEAWEEDFS_SECRET_KEY"] = seaweedfs_secret_key

    openclaw_channels: tuple[str, ...] = ()
    my_farm_advisor_channels: tuple[str, ...] = ()
    if _prompt_yes_no(prompt, "Enable OpenClaw? [Y/n]: ", default=True):
        selected.append("openclaw")
        default_openclaw_channel = "matrix" if "matrix" in selected else "telegram"
        raw_channels = _prompt_default(
            prompt,
            "OpenClaw channels [telegram/matrix] "
            f"(comma separated, default: {default_openclaw_channel}): ",
            default=default_openclaw_channel,
        )
        openclaw_channels = tuple(
            sorted({item.strip() for item in raw_channels.split(",") if item.strip()})
        )
        if "matrix" in openclaw_channels and "matrix" not in selected:
            selected.append("matrix")
        advisor_env.update(
            _prompt_advisor_runtime_config(
                prompt=prompt,
                label="OpenClaw",
                env_prefix="OPENCLAW",
            )
        )
        advisor_env.update(
            _prompt_advisor_telegram_config(
                prompt=prompt,
                label="OpenClaw",
                env_prefix="OPENCLAW",
                channels=openclaw_channels,
            )
        )
    else:
        disabled.append("openclaw")
    if _prompt_yes_no(prompt, "Enable My Farm Advisor? [y/N]: ", default=False):
        selected.append("my-farm-advisor")
        raw_channels = _read_prompt(
            prompt,
            "My Farm Advisor channels [telegram/matrix] (comma separated, optional): ",
        ).strip()
        if raw_channels != "":
            my_farm_advisor_channels = tuple(
                sorted({item.strip() for item in raw_channels.split(",") if item.strip()})
            )
            if "matrix" in my_farm_advisor_channels and "matrix" not in selected:
                selected.append("matrix")
        advisor_env.update(
            _prompt_advisor_runtime_config(
                prompt=prompt,
                label="My Farm Advisor",
                env_prefix="MY_FARM_ADVISOR",
            )
        )
        advisor_env.update(
            _prompt_advisor_telegram_config(
                prompt=prompt,
                label="My Farm Advisor",
                env_prefix="MY_FARM_ADVISOR",
                channels=my_farm_advisor_channels,
            )
        )
    else:
        disabled.append("my-farm-advisor")

    return PromptSelection(
        selected_packs=tuple(sorted(selected)),
        disabled_packs=tuple(sorted(disabled)),
        seaweedfs_access_key=seaweedfs_access_key,
        seaweedfs_secret_key=seaweedfs_secret_key,
        generated_secrets=dict(sorted(generated_secrets.items())),
        advisor_env=dict(sorted(advisor_env.items())),
        openclaw_channels=openclaw_channels,
        my_farm_advisor_channels=my_farm_advisor_channels,
    )


def _prompt_advisor_runtime_config(
    *, prompt: PromptFn, label: str, env_prefix: str
) -> dict[str, str]:
    values: dict[str, str] = {}
    if _prompt_yes_no(
        prompt, f"Configure a separate NVIDIA API key for {label}? [Y/n]: ", default=True
    ):
        values[f"{env_prefix}_NVIDIA_API_KEY"] = _prompt_non_empty(
            prompt, f"{label} NVIDIA API key: "
        )
    if _prompt_yes_no(
        prompt, f"Configure a separate OpenRouter API key for {label}? [Y/n]: ", default=True
    ):
        values[f"{env_prefix}_OPENROUTER_API_KEY"] = _prompt_non_empty(
            prompt, f"{label} OpenRouter API key: "
        )
    primary_default = (
        _DEFAULT_NVIDIA_PRIMARY_MODEL if f"{env_prefix}_NVIDIA_API_KEY" in values else ""
    )
    primary_model = _prompt_optional(
        prompt,
        f"{label} primary model (provider/model; optional{f', default: {primary_default}' if primary_default else ''}): ",
    )
    if primary_model is None and primary_default:
        primary_model = primary_default
    if primary_model is not None:
        values[f"{env_prefix}_PRIMARY_MODEL"] = primary_model
    fallback_models = _prompt_optional(
        prompt,
        f"{label} backup models (comma separated provider/model refs, optional, default: {_DEFAULT_OPENROUTER_FALLBACK_MODEL}): ",
    )
    if fallback_models is None:
        fallback_models = _DEFAULT_OPENROUTER_FALLBACK_MODEL
    if fallback_models is not None:
        values[f"{env_prefix}_FALLBACK_MODELS"] = ",".join(
            item.strip() for item in fallback_models.split(",") if item.strip()
        )
    return values


def _prompt_advisor_telegram_config(
    *, prompt: PromptFn, label: str, env_prefix: str, channels: tuple[str, ...]
) -> dict[str, str]:
    if "telegram" not in channels:
        return {}
    values: dict[str, str] = {}
    values[f"{env_prefix}_TELEGRAM_BOT_TOKEN"] = _prompt_non_empty(
        prompt,
        f"{label} Telegram bot token: ",
    )
    owner_id = _prompt_optional(
        prompt,
        f"{label} Telegram owner user ID (numeric sender id; @username resolves to id, optional): ",
    )
    if owner_id is not None:
        values[f"{env_prefix}_TELEGRAM_OWNER_USER_ID"] = owner_id
    return values


def prompt_for_initial_install_values(
    prompt: PromptFn = input,
    *,
    require_dokploy_auth: bool = True,
    output: Callable[[str], None] = print,
) -> GuidedInstallValues:
    root_domain = _prompt_non_empty(prompt, "Root domain: ")
    stack_name = _prompt_default(
        prompt,
        f"Stack name (default: {_suggest_stack_name(root_domain)}): ",
        default=_suggest_stack_name(root_domain),
    )
    dokploy_subdomain = _prompt_default(
        prompt,
        "Dokploy subdomain (default: dokploy): ",
        default="dokploy",
    )
    dokploy_admin_email = _prompt_default(
        prompt,
        f"Dokploy admin email (default: {_DEFAULT_DOKPLOY_ADMIN_EMAIL}): ",
        default=_DEFAULT_DOKPLOY_ADMIN_EMAIL,
    )
    dokploy_admin_password = None
    if require_dokploy_auth:
        dokploy_admin_password = _prompt_default(
            prompt,
            "Dokploy admin password (used locally to sign in or create the first admin "
            "and mint an API key; default: ChangeMeSoon): ",
            default="ChangeMeSoon",
        )

    private_network_mode = _prompt_choice(
        prompt,
        "Private network mode [headscale/tailscale/none] (default: headscale): ",
        choices=("headscale", "tailscale", "none"),
        default="headscale",
    )
    enable_headscale = private_network_mode == "headscale"
    enable_tailscale = private_network_mode == "tailscale"

    tailscale_auth_key: str | None = None
    tailscale_hostname: str | None = None
    tailscale_enable_ssh = False
    tailscale_tags: tuple[str, ...] = ()
    tailscale_subnet_routes: tuple[str, ...] = ()
    if enable_tailscale:
        tailscale_auth_key = _prompt_non_empty(
            prompt,
            "Tailscale auth key (from the Tailscale admin console; use a key that lets "
            "this host join your tailnet): ",
        )
        tailscale_hostname = _prompt_non_empty(prompt, "Tailscale hostname: ")
        tailscale_enable_ssh = _prompt_yes_no(
            prompt,
            "Enable Tailscale SSH for this host? [y/N]: ",
            default=False,
        )
        raw_tags = _read_prompt(
            prompt,
            "Tailscale tags (comma separated tag:... values, optional): ",
        ).strip()
        if raw_tags != "":
            tailscale_tags = tuple(
                sorted({item.strip() for item in raw_tags.split(",") if item.strip()})
            )
        raw_routes = _read_prompt(
            prompt,
            "Tailscale subnet routes (comma separated CIDRs, optional): ",
        ).strip()
        if raw_routes != "":
            tailscale_subnet_routes = tuple(
                sorted({item.strip() for item in raw_routes.split(",") if item.strip()})
            )

    if _prompt_yes_no(
        prompt,
        "Need help finding your Cloudflare token, account ID, and zone ID? [y/N]: ",
        default=False,
    ):
        _emit_cloudflare_help(output)

    return GuidedInstallValues(
        stack_name=stack_name,
        root_domain=root_domain,
        dokploy_subdomain=dokploy_subdomain,
        dokploy_admin_email=dokploy_admin_email,
        dokploy_admin_password=dokploy_admin_password,
        enable_headscale=enable_headscale,
        cloudflare_api_token=_prompt_non_empty(prompt, "Cloudflare API token: "),
        cloudflare_account_id=_prompt_non_empty(prompt, "Cloudflare account ID: "),
        cloudflare_zone_id=_prompt_optional(
            prompt,
            f"Cloudflare zone ID (optional; press Enter to look up from {root_domain}): ",
        ),
        enable_tailscale=enable_tailscale,
        tailscale_auth_key=tailscale_auth_key,
        tailscale_hostname=tailscale_hostname,
        tailscale_enable_ssh=tailscale_enable_ssh,
        tailscale_tags=tailscale_tags,
        tailscale_subnet_routes=tailscale_subnet_routes,
    )


def _prompt_yes_no(prompt: PromptFn, message: str, *, default: bool) -> bool:
    response = _read_prompt(prompt, message).strip().lower()
    if response == "":
        return default
    if response in {"y", "yes"}:
        return True
    if response in {"n", "no"}:
        return False
    raise StateValidationError(f"Invalid yes/no response: {response!r}.")


def _prompt_choice(
    prompt: PromptFn,
    message: str,
    *,
    choices: tuple[str, ...],
    default: str,
) -> str:
    response = _read_prompt(prompt, message).strip().lower()
    if response == "":
        return default
    if response not in choices:
        raise StateValidationError(f"Invalid selection {response!r}; expected one of {choices}.")
    return response


def _prompt_non_empty(prompt: PromptFn, message: str) -> str:
    response = _read_prompt(prompt, message).strip()
    if response == "":
        raise StateValidationError(f"Prompted value for {message.strip()!r} cannot be empty.")
    return response


def _prompt_optional(prompt: PromptFn, message: str) -> str | None:
    response = _read_prompt(prompt, message).strip()
    return response or None


def _prompt_default(prompt: PromptFn, message: str, *, default: str) -> str:
    response = _read_prompt(prompt, message).strip()
    if response == "":
        return default
    return response


def _read_prompt(prompt: PromptFn, message: str) -> str:
    return sanitize_prompt_response(prompt(message))


def sanitize_prompt_response(response: str) -> str:
    sanitized = response.replace("\x1b[200~", "").replace("\x1b[201~", "")
    sanitized = _ANSI_OSC_RE.sub("", sanitized)
    sanitized = _ANSI_CSI_RE.sub("", sanitized)
    sanitized = _CARET_CSI_RE.sub("", sanitized)
    sanitized = "".join(
        character for character in sanitized if character >= " " or character == "\t"
    )
    return sanitized


def _generate_credential(*, prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(12)}"


def _emit_cloudflare_help(output: Callable[[str], None]) -> None:
    output("")
    output("Cloudflare setup help")
    output("1. Create the token")
    output("   URL: https://dash.cloudflare.com/profile/api-tokens")
    output("   Click path:")
    output("     Create Token")
    output("     Create Custom Token")
    output("   Minimum token permissions for this wizard:")
    output("     Account -> Cloudflare Tunnel -> Edit")
    output("     Zone -> DNS -> Edit")
    output("     Account -> Access: Apps and Policies -> Edit")
    output("     Account -> Access: Organizations, Identity Providers, and Groups -> Edit")
    output("")
    output("2. Account ID")
    output("   What it is:")
    output("     The Cloudflare account that owns tunnel and Access resources.")
    output("   Where to find it:")
    output("     Cloudflare dashboard")
    output("     Account home")
    output("     Your account row")
    output("     Copy account ID")
    output("")
    output("3. Zone ID")
    output("   What it is:")
    output("     The DNS zone ID for your root domain.")
    output("   Where to find it:")
    output("     Cloudflare dashboard")
    output("     Your domain")
    output("     Overview")
    output("     API section")
    output("     Zone ID")
    output("   If you are unsure which zone to use:")
    output("     Use the root domain itself.")
    output("     Good: openmerge.me")
    output("     Not this: dokploy.openmerge.me")
    output("")
    output("4. Official help if you still need it")
    output(
        "   Token docs: https://developers.cloudflare.com/fundamentals/api/get-started/create-token/"
    )
    output(
        "   Account ID / Zone ID docs: https://developers.cloudflare.com/fundamentals/account/find-account-and-zone-ids/"
    )
    output("")


def _suggest_stack_name(root_domain: str) -> str:
    primary_label = root_domain.split(".", 1)[0].strip().lower()
    normalized = "".join(
        character if character.isalnum() or character == "-" else "-" for character in primary_label
    ).strip("-")
    return normalized or "dokploy-stack"
