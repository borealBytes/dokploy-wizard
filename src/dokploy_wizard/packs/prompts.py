# ruff: noqa: E501
"""Minimal CLI helpers for guided pack selection."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from dokploy_wizard.packs.catalog import get_pack_definition
from dokploy_wizard.state.env import derive_stack_name_from_root_domain
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


_DEFAULT_AI_DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
_DEFAULT_DOKPLOY_ADMIN_EMAIL = "admin@example.com"


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
    seaweedfs_access_key: str | None = None
    seaweedfs_secret_key: str | None = None
    generated_secrets: dict[str, str] = {}
    if _prompt_yes_no(prompt, "Enable SeaweedFS object storage? [Y/n]: ", default=True):
        selected.append("seaweedfs")
        seaweedfs_access_key = _generate_credential(prefix="seaweed")
        seaweedfs_secret_key = _generate_credential(prefix="seaweed-secret")
        generated_secrets["SEAWEEDFS_ACCESS_KEY"] = seaweedfs_access_key
        generated_secrets["SEAWEEDFS_SECRET_KEY"] = seaweedfs_secret_key
    # Gitea is a first-class pack selected through PACKS (the legacy
    # ENABLE_GITEA flag is a deprecated one-release alias). It rides the
    # SeaweedFS bucket for LFS/attachments: a yes here pulls the storage layer
    # in through dependency expansion even when the answer above was no, and
    # the deploy then synthesizes the S3 credentials into state like any other
    # expansion-enabled storage pack.
    if _prompt_yes_no(prompt, "Enable Gitea? [Y/n]: ", default=True):
        selected.append("gitea")

    return PromptSelection(
        selected_packs=tuple(sorted(selected)),
        disabled_packs=tuple(sorted(disabled)),
        seaweedfs_access_key=seaweedfs_access_key,
        seaweedfs_secret_key=seaweedfs_secret_key,
        generated_secrets=dict(sorted(generated_secrets.items())),
    )


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
        "Default AI API key for Hermes and K-Dense BYOK (optional; press Enter to skip): ",
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
    return derive_stack_name_from_root_domain(root_domain)
