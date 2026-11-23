# ruff: noqa: E501
"""Deterministic pack selection parsing and dependency resolution."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Mapping

from dokploy_wizard.packs.catalog import (
    PackDefinition,
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
    _expand_dependencies(enabled, explicit_disabled=explicit_disabled, values=values)
    _validate_slots(enabled)

    enabled_features = {"dokploy"}
    hostnames: dict[str, str] = {}
    hostname_targets: dict[str, str] = {}
    for pack_name in sorted(enabled):
        pack = get_pack_definition(pack_name)
        enabled_features.update(pack.enabled_features)
        for hostname in pack.hostnames:
            fqdn = _join_hostname(
                _get_configured_value(values, hostname.env_key) or hostname.default_subdomain,
                root_domain,
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

    return ResolvedPackSelection(
        selected_packs=tuple(sorted(explicit_selected)),
        enabled_packs=tuple(sorted(enabled)),
        enabled_features=tuple(sorted(enabled_features)),
        hostnames=dict(sorted(hostnames.items())),
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
        if pack.env_flag_deprecated:
            _warn_deprecated_env_flag(pack)
        if enabled:
            explicit_selected.add(pack.name)
            explicit_disabled.discard(pack.name)
        else:
            explicit_disabled.add(pack.name)
            explicit_selected.discard(pack.name)

    return explicit_selected, explicit_disabled


def _warn_deprecated_env_flag(pack: PackDefinition) -> None:
    print(
        (
            f"Warning: the {pack.env_flag} flag is deprecated; list '{pack.name}' in PACKS "
            f"instead. The flag still resolves the '{pack.name}' pack this release and stops "
            "being read in the next one."
        ),
        file=sys.stderr,
    )


def _expand_dependencies(
    enabled: set[str], *, explicit_disabled: set[str], values: Mapping[str, str]
) -> None:
    pending = sorted(enabled)
    while pending:
        pack_name = pending.pop(0)
        pack = get_pack_definition(pack_name)
        for dependency in pack.depends_on:
            if dependency == "headscale" and _uses_existing_tailscale_control_plane(values):
                continue
            if dependency in explicit_disabled:
                raise StateValidationError(
                    f"Pack '{pack_name}' requires '{dependency}', but '{dependency}' was explicitly disabled."
                )
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


def _get_csv(values: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw_value = values.get(key)
    if raw_value is None:
        return ()
    items = {item.strip() for item in raw_value.split(",") if item.strip() != ""}
    return tuple(sorted(items))


def _get_configured_value(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized


def _parse_bool(raw_value: str, key: str) -> bool:
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise StateValidationError(f"Invalid boolean value for '{key}': {raw_value!r}.")
