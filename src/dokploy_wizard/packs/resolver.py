"""Deterministic pack selection parsing and dependency resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from dokploy_wizard.packs.catalog import (
    get_known_pack_names,
    get_pack_definition,
    iter_pack_catalog,
)
from dokploy_wizard.state.models import StateValidationError


@dataclass(frozen=True)
class ResolvedPackSelection:
    selected_packs: tuple[str, ...]
    enabled_packs: tuple[str, ...]
    enabled_features: tuple[str, ...]
    hostnames: dict[str, str]
    access_wrapped_hostnames: dict[str, str]
    machine_hostnames: dict[str, str]
    workspace_daemon_packs: tuple[str, ...]
    openclaw_channels: tuple[str, ...]
    my_farm_advisor_channels: tuple[str, ...]


def has_explicit_pack_selection(values: Mapping[str, str]) -> bool:
    if "PACKS" in values:
        return True
    return any(pack.env_flag in values for pack in iter_pack_catalog())


def resolve_pack_selection(values: Mapping[str, str], *, root_domain: str) -> ResolvedPackSelection:
    explicit_selected, explicit_disabled = _parse_explicit_pack_intent(values)
    enabled = {
        pack.name
        for pack in iter_pack_catalog()
        if pack.default_enabled and pack.name not in explicit_disabled
    }
    enabled.update(explicit_selected)
    _expand_dependencies(enabled, values=values)
    _validate_slots(enabled)

    enabled_features = {"dokploy"}
    hostnames: dict[str, str] = {}
    access_wrapped_hostnames: dict[str, str] = {}
    machine_hostnames: dict[str, str] = {}
    workspace_daemon_packs: set[str] = set()
    hostname_targets: dict[str, str] = {}
    for pack_name in sorted(enabled):
        pack = get_pack_definition(pack_name)
        enabled_features.update(pack.enabled_features)
        if pack.workspace_daemon_enabled:
            workspace_daemon_packs.add(pack.name)
        for hostname in pack.hostnames:
            fqdn = _join_hostname(
                values.get(hostname.env_key, hostname.default_subdomain), root_domain
            )
            existing_key = hostname_targets.get(fqdn)
            if existing_key is not None and existing_key != hostname.key:
                msg = (
                    f"Hostname collision: '{hostname.key}' and '{existing_key}' both "
                    f"resolve to '{fqdn}'."
                )
                raise StateValidationError(msg)
            hostname_targets[fqdn] = hostname.key
            hostnames[hostname.key] = fqdn
            if hostname.access_wrapped:
                access_wrapped_hostnames[hostname.key] = fqdn
            if hostname.route_kind == "machine":
                machine_hostnames[hostname.key] = fqdn

    openclaw_channels: tuple[str, ...] = ()
    my_farm_advisor_channels: tuple[str, ...] = ()
    if "openclaw" in enabled:
        openclaw_channels = _validate_advisor_channels(
            values, enabled, key="OPENCLAW_CHANNELS", pack_name="openclaw"
        )
    elif "OPENCLAW_CHANNELS" in values:
        raise StateValidationError("OPENCLAW_CHANNELS requires the 'openclaw' pack.")
    if "my-farm-advisor" in enabled:
        my_farm_advisor_channels = _validate_advisor_channels(
            values,
            enabled,
            key="MY_FARM_ADVISOR_CHANNELS",
            pack_name="my-farm-advisor",
        )
    elif "MY_FARM_ADVISOR_CHANNELS" in values:
        raise StateValidationError("MY_FARM_ADVISOR_CHANNELS requires the 'my-farm-advisor' pack.")

    return ResolvedPackSelection(
        selected_packs=tuple(sorted(explicit_selected)),
        enabled_packs=tuple(sorted(enabled)),
        enabled_features=tuple(sorted(enabled_features)),
        hostnames=dict(sorted(hostnames.items())),
        access_wrapped_hostnames=dict(sorted(access_wrapped_hostnames.items())),
        machine_hostnames=dict(sorted(machine_hostnames.items())),
        workspace_daemon_packs=tuple(sorted(workspace_daemon_packs)),
        openclaw_channels=openclaw_channels,
        my_farm_advisor_channels=my_farm_advisor_channels,
    )


def _parse_explicit_pack_intent(values: Mapping[str, str]) -> tuple[set[str], set[str]]:
    explicit_selected = set(_get_csv(values, "PACKS"))
    explicit_disabled: set[str] = set()
    known_packs = set(get_known_pack_names())
    unknown = sorted(explicit_selected - known_packs)
    if unknown:
        msg = f"Unknown pack selection(s) in PACKS: {unknown}."
        raise StateValidationError(msg)

    for pack in iter_pack_catalog():
        raw_value = values.get(pack.env_flag)
        if raw_value is None:
            continue
        enabled = _parse_bool(raw_value, pack.env_flag)
        if enabled:
            explicit_selected.add(pack.name)
            explicit_disabled.discard(pack.name)
        else:
            explicit_disabled.add(pack.name)
            explicit_selected.discard(pack.name)

    return explicit_selected, explicit_disabled


def _expand_dependencies(enabled: set[str], *, values: Mapping[str, str]) -> None:
    pending = sorted(enabled)
    while pending:
        pack_name = pending.pop(0)
        pack = get_pack_definition(pack_name)
        for dependency in pack.depends_on:
            if dependency == "headscale" and _uses_existing_tailscale_control_plane(values):
                continue
            if dependency not in enabled:
                enabled.add(dependency)
                pending.append(dependency)
                pending.sort()


def _uses_existing_tailscale_control_plane(values: Mapping[str, str]) -> bool:
    tailscale_enabled = values.get("ENABLE_TAILSCALE")
    headscale_enabled = values.get("ENABLE_HEADSCALE")
    if tailscale_enabled is None or headscale_enabled is None:
        return False
    return _parse_bool(tailscale_enabled, "ENABLE_TAILSCALE") and not _parse_bool(
        headscale_enabled, "ENABLE_HEADSCALE"
    )


def _validate_slots(enabled: set[str]) -> None:
    slots: dict[str, list[str]] = {}
    for pack_name in sorted(enabled):
        slot = get_pack_definition(pack_name).slot
        if slot is None:
            continue
        slots.setdefault(slot, []).append(pack_name)

    conflicting = {slot: names for slot, names in slots.items() if len(names) > 1}
    if conflicting:
        slot, names = next(iter(sorted(conflicting.items())))
        msg = (
            f"Invalid pack selection: {names} all occupy the '{slot}' slot and cannot be "
            "enabled together."
        )
        raise StateValidationError(msg)


def _join_hostname(subdomain: str, root_domain: str) -> str:
    return f"{subdomain}.{root_domain}".lower()


def _validate_advisor_channels(
    values: Mapping[str, str], enabled: set[str], *, key: str, pack_name: str
) -> tuple[str, ...]:
    channels = _get_csv(values, key)
    supported_channels = {"telegram", "matrix"}
    invalid_channels = sorted(channel for channel in channels if channel not in supported_channels)
    if invalid_channels:
        raise StateValidationError(
            f"Unsupported {pack_name} channel selection(s): "
            f"{invalid_channels}. Supported values are ['matrix', 'telegram']."
        )
    if "matrix" in channels and "matrix" not in enabled:
        raise StateValidationError(
            f"{key} may include 'matrix' only when the Matrix pack is enabled."
        )
    return channels


def _get_csv(values: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw_value = values.get(key)
    if raw_value is None:
        return ()
    items = {item.strip() for item in raw_value.split(",") if item.strip() != ""}
    return tuple(sorted(items))


def _parse_bool(raw_value: str, key: str) -> bool:
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise StateValidationError(f"Invalid boolean value for '{key}': {raw_value!r}.")
