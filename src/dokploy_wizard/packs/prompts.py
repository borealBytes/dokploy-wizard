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


_RUNTIME_APP_ENV_KEYS = (
    "OPENCLAW_GATEWAY_PASSWORD",
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
    "MY_FARM_ADVISOR_GATEWAY_PASSWORD",
)
_DEFAULT_NVIDIA_PRIMARY_MODEL = "nvidia/moonshotai/kimi-k2.5"
_DEFAULT_AI_DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
_DEFAULT_REMOTE_FALLBACK_MODEL = "opencode-go/deepseek-v4-flash"
_DEFAULT_DOKPLOY_ADMIN_EMAIL = "clayton@superiorbyteworks.com"
_DEFAULT_MY_FARM_CHANNEL = "telegram"
_OPENCLAW_RUNTIME_ENV_KEYS = tuple(
    key for key in _RUNTIME_APP_ENV_KEYS if key.startswith("OPENCLAW_")
)
_MY_FARM_ADVISOR_RUNTIME_ENV_KEYS = tuple(
    key for key in _RUNTIME_APP_ENV_KEYS if key.startswith("MY_FARM_ADVISOR_")
)
_MY_FARM_ADVISOR_PACK_ONLY_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "NVIDIA_BASE_URL",
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
)


@dataclass(frozen=True)
class GuidedInstallValues:
    stack_name: str
    root_domain: str
    dokploy_subdomain: str
    dokploy_admin_email: str
    dokploy_admin_password: str | None
    ai_default_api_key: str | None
    ai_default_base_url: str | None
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
        for key in _OPENCLAW_RUNTIME_ENV_KEYS:
            updated_values.pop(key, None)
        updated_values.pop("OPENCLAW_GATEWAY_TOKEN", None)
    if "my-farm-advisor" in selection.disabled_packs:
        for key in _MY_FARM_ADVISOR_RUNTIME_ENV_KEYS:
            updated_values.pop(key, None)
        for key in _MY_FARM_ADVISOR_PACK_ONLY_ENV_KEYS:
            updated_values.pop(key, None)
    resulting_packs = {
        item.strip() for item in updated_values.get("PACKS", "").split(",") if item.strip()
    }
    updated_values.update(selection.advisor_env)
    return RawEnvInput(format_version=raw_env.format_version, values=updated_values)


def prompt_for_pack_selection(
    prompt: PromptFn = input,
    *,
    include_headscale_prompt: bool = True,
    headscale_default: bool = True,
    shared_ai_default_configured: bool = False,
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
        advisor_env.setdefault(
            "OPENCLAW_GATEWAY_PASSWORD", _generate_credential(prefix="openclaw-ui")
        )
        generated_secrets.setdefault(
            "OPENCLAW_GATEWAY_PASSWORD", advisor_env["OPENCLAW_GATEWAY_PASSWORD"]
        )
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
                shared_ai_default_configured=shared_ai_default_configured,
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
        advisor_env.setdefault(
            "MY_FARM_ADVISOR_GATEWAY_PASSWORD", _generate_credential(prefix="my-farm-ui")
        )
        generated_secrets.setdefault(
            "MY_FARM_ADVISOR_GATEWAY_PASSWORD",
            advisor_env["MY_FARM_ADVISOR_GATEWAY_PASSWORD"],
        )
        raw_channels = _prompt_default(
            prompt,
            "My Farm Advisor channels [telegram/matrix] "
            f"(comma separated, default: {_DEFAULT_MY_FARM_CHANNEL}): ",
            default=_DEFAULT_MY_FARM_CHANNEL,
        )
        my_farm_advisor_channels = _parse_channel_list(raw_channels)
        if "matrix" in my_farm_advisor_channels and "matrix" not in selected:
            selected.append("matrix")
        advisor_env.update(
            _prompt_advisor_runtime_config(
                prompt=prompt,
                label="My Farm Advisor",
                env_prefix="MY_FARM_ADVISOR",
                shared_ai_default_configured=shared_ai_default_configured,
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
        advisor_env.update(_prompt_my_farm_advisor_runtime_extras(prompt=prompt))
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
    *,
    prompt: PromptFn,
    label: str,
    env_prefix: str,
    shared_ai_default_configured: bool,
) -> dict[str, str]:
    values: dict[str, str] = {}
    if _prompt_yes_no(
        prompt,
        f"Configure a separate NVIDIA API key for {label}? [{'y/N' if shared_ai_default_configured else 'Y/n'}]: ",
        default=not shared_ai_default_configured,
    ):
        values[f"{env_prefix}_NVIDIA_API_KEY"] = _prompt_non_empty(
            prompt, f"{label} NVIDIA API key: "
        )
    if _prompt_yes_no(
        prompt,
        f"Configure a separate OpenAI-compatible fallback API key for {label}? [{'y/N' if shared_ai_default_configured else 'Y/n'}]: ",
        default=not shared_ai_default_configured,
    ):
        values[f"{env_prefix}_OPENROUTER_API_KEY"] = _prompt_non_empty(
            prompt, f"{label} fallback API key: "
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
        f"{label} backup models (comma separated provider/model refs, optional, default: {_DEFAULT_REMOTE_FALLBACK_MODEL}): ",
    )
    if fallback_models is None:
        fallback_models = _DEFAULT_REMOTE_FALLBACK_MODEL
    if fallback_models is not None:
        values[f"{env_prefix}_FALLBACK_MODELS"] = ",".join(
            item.strip() for item in fallback_models.split(",") if item.strip()
        )
    return values


def _prompt_my_farm_advisor_runtime_extras(*, prompt: PromptFn) -> dict[str, str]:
    values: dict[str, str] = {}
    anthropic_api_key = _prompt_optional(
        prompt,
        "My Farm Advisor Anthropic API key (optional; press Enter to skip): ",
    )
    if anthropic_api_key is not None:
        values["ANTHROPIC_API_KEY"] = anthropic_api_key
    nvidia_base_url = _prompt_optional(
        prompt,
        "My Farm Advisor NVIDIA base URL override (optional; press Enter to skip): ",
    )
    if nvidia_base_url is not None:
        values["NVIDIA_BASE_URL"] = nvidia_base_url
    values.update(
        _prompt_optional_key_values(
            prompt,
            (
                (
                    "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
                    "Field operations Telegram bot token",
                ),
                (
                    "TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE",
                    "Field operations Telegram bot pairing code",
                ),
                (
                    "TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS",
                    "Field operations Telegram allowed users (comma separated user IDs/usernames)",
                ),
                (
                    "TELEGRAM_DATA_PIPELINE_BOT_TOKEN",
                    "Data pipeline Telegram bot token",
                ),
                (
                    "TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE",
                    "Data pipeline Telegram bot pairing code",
                ),
                (
                    "TELEGRAM_DATA_PIPELINE_ALLOWED_USERS",
                    "Data pipeline Telegram allowed users (comma separated user IDs/usernames)",
                ),
                (
                    "TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS",
                    "Data pipeline bot allowlist (comma separated user IDs/usernames)",
                ),
                (
                    "TELEGRAM_ALLOWED_USERS",
                    "Global Telegram allowed users (comma separated user IDs/usernames)",
                ),
                (
                    "OPENCLAW_TELEGRAM_GROUP_POLICY",
                    "Telegram group policy override",
                ),
                ("TZ", "Timezone override (for example UTC or America/Chicago)"),
                (
                    "OPENCLAW_BOOTSTRAP_REFRESH",
                    "Bootstrap refresh override (for example 1/0)",
                ),
                (
                    "OPENCLAW_MEMORY_SEARCH_ENABLED",
                    "Memory search override (for example 1/0)",
                ),
            ),
        )
    )
    if _prompt_yes_no(
        prompt,
        "Configure optional My Farm Advisor R2/data settings? [y/N]: ",
        default=False,
    ):
        values.update(
            _prompt_optional_key_values(
                prompt,
                (
                    ("R2_BUCKET_NAME", "R2 bucket name"),
                    ("R2_ENDPOINT", "R2 endpoint URL (optional if you plan to use CF account ID)"),
                    ("R2_ACCESS_KEY_ID", "R2 access key ID"),
                    ("R2_SECRET_ACCESS_KEY", "R2 secret access key"),
                    ("CF_ACCOUNT_ID", "Cloudflare account ID override for R2"),
                    ("DATA_MODE", "Data mode override (for example r2)"),
                    (
                        "WORKSPACE_DATA_R2_RCLONE_MOUNT",
                        "Enable workspace R2 rclone mount (for example 1/true)",
                    ),
                    (
                        "WORKSPACE_DATA_R2_PREFIX",
                        "Workspace data R2 prefix",
                    ),
                    (
                        "OPENCLAW_SYNC_SKILLS_ON_START",
                        "Sync skills on start override (for example 1/0)",
                    ),
                    (
                        "OPENCLAW_SYNC_SKILLS_OVERWRITE",
                        "Sync skills overwrite override (for example 1/0)",
                    ),
                    (
                        "OPENCLAW_FORCE_SKILL_SYNC",
                        "Force skill sync override (for example 1/0)",
                    ),
                ),
            )
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


def _prompt_optional_key_values(
    prompt: PromptFn, fields: tuple[tuple[str, str], ...]
) -> dict[str, str]:
    values: dict[str, str] = {}
    for env_key, label in fields:
        response = _prompt_optional(prompt, f"{label} (optional): ")
        if response is not None:
            values[env_key] = response
    return values


def _parse_channel_list(raw_channels: str) -> tuple[str, ...]:
    return tuple(sorted({item.strip() for item in raw_channels.split(",") if item.strip()}))


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

    cloudflare_api_token = _prompt_non_empty(prompt, "Cloudflare API token: ")
    cloudflare_account_id = _prompt_non_empty(prompt, "Cloudflare account ID: ")
    cloudflare_zone_id = _prompt_optional(
        prompt,
        f"Cloudflare zone ID (optional; press Enter to look up from {root_domain}): ",
    )

    ai_default_api_key = _prompt_optional(
        prompt,
        "Default AI API key for Hermes, K-Dense BYOK, and advisor backup models (optional; press Enter to skip): ",
    )
    ai_default_base_url = None
    if ai_default_api_key is not None:
        ai_default_base_url = _prompt_default(
            prompt,
            f"Default AI base URL (default: {_DEFAULT_AI_DEFAULT_BASE_URL}): ",
            default=_DEFAULT_AI_DEFAULT_BASE_URL,
        )

    return GuidedInstallValues(
        stack_name=stack_name,
        root_domain=root_domain,
        dokploy_subdomain=dokploy_subdomain,
        dokploy_admin_email=dokploy_admin_email,
        dokploy_admin_password=dokploy_admin_password,
        ai_default_api_key=ai_default_api_key,
        ai_default_base_url=ai_default_base_url,
        enable_headscale=enable_headscale,
        cloudflare_api_token=cloudflare_api_token,
        cloudflare_account_id=cloudflare_account_id,
        cloudflare_zone_id=cloudflare_zone_id,
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
    output("     Cloudflare may show this in token summaries as Cloudflare One Connectors/cloudflared")
    output("     Zone -> DNS -> Edit")
    output("     Account -> Access: Apps and Policies -> Edit")
    output("     Account -> Access: Organizations, Identity Providers, and Groups -> Edit")
    output("   If you want nested Coder app hosts like *.coder.<root-domain>:")
    output("     Zone -> SSL and Certificates -> Edit")
    output("     Advanced Certificate Manager must be enabled for the zone")
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
