"""Env-file parsing and desired-state resolution."""

from __future__ import annotations

import re
from pathlib import Path
from urllib import parse

from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.packs.resolver import resolve_pack_selection
from dokploy_wizard.state.models import DesiredState, RawEnvInput, StateValidationError

_ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


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
        if value == "":
            msg = f"Invalid env value for '{key}' on line {line_number}: cannot be empty."
            raise StateValidationError(msg)
        if key in values:
            msg = f"Duplicate env key '{key}' on line {line_number}."
            raise StateValidationError(msg)
        values[key] = value

    return RawEnvInput(format_version=1, values=values)


def resolve_desired_state(raw_env: RawEnvInput) -> DesiredState:
    values = raw_env.values
    stack_name = _require_value(values, "STACK_NAME")
    root_domain = _require_value(values, "ROOT_DOMAIN")
    dokploy_subdomain = values.get("DOKPLOY_SUBDOMAIN", "dokploy")
    hostnames: dict[str, str] = {
        "dokploy": _join_hostname(dokploy_subdomain, root_domain),
    }
    pack_selection = resolve_pack_selection(values, root_domain=root_domain)
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
        openclaw_channels=pack_selection.openclaw_channels,
        openclaw_replicas=_resolve_openclaw_replicas(values, pack_selection.enabled_packs),
        my_farm_advisor_channels=pack_selection.my_farm_advisor_channels,
        my_farm_advisor_replicas=_resolve_pack_replicas(
            values,
            pack_selection.enabled_packs,
            key="MY_FARM_ADVISOR_REPLICAS",
            pack_name="my-farm-advisor",
        ),
        shared_core=build_shared_core_plan(stack_name, pack_selection.enabled_packs),
    )


def _join_hostname(subdomain: str, root_domain: str) -> str:
    return f"{subdomain}.{root_domain}".lower()


def _require_value(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if value is None:
        msg = f"Missing required env key '{key}'."
        raise StateValidationError(msg)
    return value


def _resolve_dokploy_api_url(values: dict[str, str]) -> str | None:
    raw_url = values.get("DOKPLOY_API_URL")
    raw_key = values.get("DOKPLOY_API_KEY")
    if raw_url is None and raw_key is None:
        return None
    if raw_key is None:
        raise StateValidationError("DOKPLOY_API_URL and DOKPLOY_API_KEY must be provided together.")
    if raw_url is None:
        return "https://" + _join_hostname(
            values.get("DOKPLOY_SUBDOMAIN", "dokploy"),
            _require_value(values, "ROOT_DOMAIN"),
        )
    parsed = parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc == "":
        raise StateValidationError(
            f"DOKPLOY_API_URL must be an absolute http(s) URL, found {raw_url!r}."
        )
    return raw_url.rstrip("/")


def _resolve_tailscale_enabled(values: dict[str, str]) -> bool:
    raw_value = values.get("ENABLE_TAILSCALE")
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
    raw_value = values.get("TAILSCALE_ENABLE_SSH")
    if raw_value is None:
        return False
    return _parse_bool(raw_value, key="TAILSCALE_ENABLE_SSH")


def _resolve_tailscale_csv(values: dict[str, str], *, key: str) -> tuple[str, ...]:
    if not _resolve_tailscale_enabled(values):
        return ()
    raw_value = values.get(key, "")
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
    raw_value = values.get("CLOUDFLARE_ACCESS_OTP_EMAILS", "")
    if raw_value == "":
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
    raw_value = values.get(key)
    if "seaweedfs" not in enabled_packs:
        if raw_value is not None:
            raise StateValidationError(f"{key} requires the 'seaweedfs' pack.")
        return None
    if raw_value is None:
        sibling = (
            "SEAWEEDFS_SECRET_KEY" if key == "SEAWEEDFS_ACCESS_KEY" else "SEAWEEDFS_ACCESS_KEY"
        )
        raise StateValidationError(
            f"{key} is required when the 'seaweedfs' pack is enabled (along with {sibling})."
        )
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
    raw_value = values.get(key)
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
