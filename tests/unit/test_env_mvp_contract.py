# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.state import parse_env_file, resolve_desired_state
from dokploy_wizard.packs.resolver import resolve_pack_selection


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_root_env():
    return parse_env_file(_repo_root() / ".install.env")


def test_root_install_env_resolves_current_mvp_pack_contract() -> None:
    raw_env = _load_root_env()
    selection = resolve_pack_selection(raw_env.values, root_domain=raw_env.values["ROOT_DOMAIN"])
    desired_state = resolve_desired_state(raw_env)
    required_packs = {"coder", "nextcloud", "openclaw", "seaweedfs"}

    assert required_packs.issubset(set(raw_env.values["PACKS"].split(",")))
    assert required_packs.issubset(set(selection.selected_packs))
    assert required_packs.issubset(set(selection.enabled_packs))
    assert required_packs.issubset(set(desired_state.selected_packs))
    assert desired_state.enabled_packs == selection.enabled_packs
    assert desired_state.enable_tailscale is True
    assert desired_state.tailscale_hostname == "openmerge"
    assert desired_state.hostnames["coder"] == "coder.openmerge.me"
    assert desired_state.hostnames["coder-wildcard"] == "*.openmerge.me"
    assert desired_state.hostnames["nextcloud"] == "nextcloud.openmerge.me"
    assert desired_state.hostnames["onlyoffice"] == "office.openmerge.me"
    assert desired_state.hostnames["openclaw"] == "openclaw.openmerge.me"
    assert desired_state.hostnames["openclaw-internal"].endswith(".openmerge.me")
    assert desired_state.hostnames["s3"] == "s3.openmerge.me"
    assert "headscale" not in desired_state.hostnames


def test_root_install_env_keeps_headscale_disabled_while_preserving_tailscale() -> None:
    raw_env = _load_root_env()
    selection = resolve_pack_selection(raw_env.values, root_domain=raw_env.values["ROOT_DOMAIN"])
    desired_state = resolve_desired_state(raw_env)

    assert raw_env.values["ENABLE_HEADSCALE"] == "false"
    assert raw_env.values["ENABLE_TAILSCALE"] == "true"
    assert "headscale" not in selection.selected_packs
    assert "headscale" not in selection.enabled_packs
    assert desired_state.selected_packs == selection.selected_packs
    assert "headscale" not in desired_state.enabled_packs
    assert desired_state.enable_tailscale is True
    assert desired_state.tailscale_hostname == "openmerge"
    assert desired_state.enabled_features == ("dokploy",)
    assert "tailscale" not in desired_state.hostnames
