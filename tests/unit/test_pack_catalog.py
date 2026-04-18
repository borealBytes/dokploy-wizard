# pyright: reportMissingImports=false

from __future__ import annotations

import pytest

from dokploy_wizard.core import build_shared_core_plan
from dokploy_wizard.packs.catalog import get_pack_definition, iter_pack_catalog
from dokploy_wizard.packs.resolver import resolve_pack_selection
from dokploy_wizard.state.models import StateValidationError
from dokploy_wizard.state import RawEnvInput, resolve_desired_state


def test_catalog_exposes_expected_pack_metadata() -> None:
    names = [pack.name for pack in iter_pack_catalog()]

    assert names == [
        "headscale",
        "matrix",
        "nextcloud",
        "seaweedfs",
        "coder",
        "openclaw",
        "my-farm-advisor",
    ]
    assert get_pack_definition("headscale").default_enabled is False
    assert get_pack_definition("seaweedfs").slot is None
    assert get_pack_definition("seaweedfs").hostnames[0].key == "s3"
    assert get_pack_definition("coder").hostnames[1].key == "coder-wildcard"
    assert get_pack_definition("openclaw").slot is None
    assert get_pack_definition("my-farm-advisor").slot is None
    assert get_pack_definition("openclaw").mutable_resource_keys == ("OPENCLAW_REPLICAS",)
    assert get_pack_definition("my-farm-advisor").mutable_resource_keys == (
        "MY_FARM_ADVISOR_REPLICAS",
    )


def test_resolver_keeps_explicit_selection_separate_from_expanded_packs() -> None:
    selection = resolve_pack_selection(
        {
            "ROOT_DOMAIN": "example.com",
            "STACK_NAME": "pack-stack",
            "ENABLE_NEXTCLOUD": "true",
        },
        root_domain="example.com",
    )

    assert selection.selected_packs == ("nextcloud",)
    assert selection.enabled_packs == ("nextcloud",)
    assert selection.enabled_features == ("dokploy",)
    assert selection.hostnames == {
        "nextcloud": "nextcloud.example.com",
        "onlyoffice": "office.example.com",
    }


def test_resolver_allows_both_advisor_packs_together() -> None:
    selection = resolve_pack_selection(
        {
            "ENABLE_OPENCLAW": "true",
            "ENABLE_MY_FARM_ADVISOR": "true",
            "ENABLE_MATRIX": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "MY_FARM_ADVISOR_CHANNELS": "telegram,matrix",
        },
        root_domain="example.com",
    )

    assert selection.enabled_packs == (
        "headscale",
        "matrix",
        "my-farm-advisor",
        "openclaw",
    )
    assert selection.openclaw_channels == ("telegram",)
    assert selection.my_farm_advisor_channels == ("matrix", "telegram")


def test_resolver_allows_existing_tailscale_to_satisfy_headscale_dependency() -> None:
    selection = resolve_pack_selection(
        {
            "ENABLE_TAILSCALE": "true",
            "ENABLE_HEADSCALE": "false",
            "ENABLE_MATRIX": "true",
        },
        root_domain="example.com",
    )

    assert selection.enabled_packs == ("matrix",)
    assert selection.hostnames == {"matrix": "matrix.example.com"}


def test_resolver_rejects_explicitly_disabled_required_dependency() -> None:
    selection = resolve_pack_selection(
        {
            "ENABLE_OPENCLAW": "true",
            "ENABLE_HEADSCALE": "false",
        },
        root_domain="example.com",
    )

    assert selection.enabled_packs == ("openclaw",)


def test_resolver_builds_root_and_wildcard_coder_hostnames() -> None:
    selection = resolve_pack_selection(
        {
            "ENABLE_CODER": "true",
        },
        root_domain="example.com",
    )

    assert selection.enabled_packs == ("coder",)
    assert selection.hostnames == {
        "coder": "coder.example.com",
        "coder-wildcard": "*.coder.example.com",
    }


def test_resolved_state_and_shared_core_use_catalog_requirements() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "catalog-stack",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "matrix,my-farm-advisor",
            },
        )
    )

    assert desired_state.selected_packs == ("matrix", "my-farm-advisor")
    assert desired_state.enabled_packs == ("headscale", "matrix", "my-farm-advisor")
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "matrix",
        "my-farm-advisor",
    ]
    assert (
        build_shared_core_plan("catalog-stack", desired_state.enabled_packs)
        == desired_state.shared_core
    )


def test_resolved_state_includes_coder_shared_core_allocation() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "coder-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
            },
        )
    )

    assert desired_state.enabled_packs == ("coder",)
    assert desired_state.hostnames["coder"] == "coder.example.com"
    assert desired_state.hostnames["coder-wildcard"] == "*.coder.example.com"
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "coder"
    ]
