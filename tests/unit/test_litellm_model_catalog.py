from __future__ import annotations

import pytest

from dokploy_wizard.litellm.model_catalog import (
    DEFAULT_LOCAL_CANONICAL_ALIAS,
    DEFAULT_LOCAL_UPSTREAM_TARGET,
    ModelCostMetadata,
    build_model_catalog,
)


def test_build_model_catalog_returns_deterministic_aliases_targets_and_costs() -> None:
    catalog = build_model_catalog(
        openrouter_model_ids=("anthropic/claude-3.5-sonnet",),
        opencode_go_model_ids=("gpt-4.1-mini",),
        nvidia_alias_targets={"nvidia/kimi-k2.5": "nvidia/moonshot/kimi-k2.5"},
        visible_aliases_by_consumer={
            "my-farm-advisor": (
                DEFAULT_LOCAL_CANONICAL_ALIAS,
                "opencode-go/gpt-4.1-mini",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            "openclaw": (
                DEFAULT_LOCAL_CANONICAL_ALIAS,
                "nvidia/kimi-k2.5",
            ),
            "coder-hermes": (DEFAULT_LOCAL_CANONICAL_ALIAS,),
            "coder-kdense": ("opencode-go/gpt-4.1-mini",),
        },
        default_alias_order=(
            DEFAULT_LOCAL_CANONICAL_ALIAS,
            "opencode-go/gpt-4.1-mini",
            "openrouter/anthropic/claude-3.5-sonnet",
            "nvidia/kimi-k2.5",
        ),
        cost_metadata_by_alias={
            "openrouter/anthropic/claude-3.5-sonnet": ModelCostMetadata(
                input_cost_per_token=0.000003,
                output_cost_per_token=0.000015,
            )
        },
    )

    assert tuple(entry.alias for entry in catalog.entries) == (
        DEFAULT_LOCAL_CANONICAL_ALIAS,
        "opencode-go/gpt-4.1-mini",
        "openrouter/anthropic/claude-3.5-sonnet",
        "nvidia/kimi-k2.5",
    )
    assert catalog.default_alias_order == (
        DEFAULT_LOCAL_CANONICAL_ALIAS,
        "opencode-go/gpt-4.1-mini",
        "openrouter/anthropic/claude-3.5-sonnet",
        "nvidia/kimi-k2.5",
    )

    local_entry = catalog.entry_for(DEFAULT_LOCAL_CANONICAL_ALIAS)
    assert local_entry.upstream_target == DEFAULT_LOCAL_UPSTREAM_TARGET
    assert local_entry.provider_slug == "tuxdesktop.tailb12aa5.ts.net"
    assert local_entry.model_id == "unsloth-active"
    assert local_entry.visible_to_consumers == (
        "coder-hermes",
        "my-farm-advisor",
        "openclaw",
    )
    assert local_entry.input_cost_per_token is None
    assert local_entry.output_cost_per_token is None

    opencode_entry = catalog.entry_for("opencode-go/gpt-4.1-mini")
    assert opencode_entry.upstream_target == "openai/gpt-4.1-mini"
    assert opencode_entry.provider_slug == "opencode-go"
    assert opencode_entry.model_id == "gpt-4.1-mini"
    assert opencode_entry.visible_to_consumers == (
        "coder-kdense",
        "my-farm-advisor",
    )

    openrouter_entry = catalog.entry_for("openrouter/anthropic/claude-3.5-sonnet")
    assert openrouter_entry.upstream_target == "openrouter/anthropic/claude-3.5-sonnet"
    assert openrouter_entry.provider_slug == "openrouter"
    assert openrouter_entry.model_id == "anthropic/claude-3.5-sonnet"
    assert openrouter_entry.input_cost_per_token == 0.000003
    assert openrouter_entry.output_cost_per_token == 0.000015

    nvidia_entry = catalog.entry_for("nvidia/kimi-k2.5")
    assert nvidia_entry.upstream_target == "nvidia/moonshotai/kimi-k2.5"
    assert nvidia_entry.provider_slug == "nvidia"
    assert nvidia_entry.model_id == "kimi-k2.5"

    assert catalog.alias_to_upstream_target() == {
        DEFAULT_LOCAL_CANONICAL_ALIAS: DEFAULT_LOCAL_UPSTREAM_TARGET,
        "opencode-go/gpt-4.1-mini": "openai/gpt-4.1-mini",
        "openrouter/anthropic/claude-3.5-sonnet": "openrouter/anthropic/claude-3.5-sonnet",
        "nvidia/kimi-k2.5": "nvidia/moonshotai/kimi-k2.5",
    }


def test_build_model_catalog_tracks_visibility_in_default_order() -> None:
    catalog = build_model_catalog(
        openrouter_model_ids=("anthropic/claude-3.5-sonnet",),
        opencode_go_model_ids=("gpt-4.1-mini", "o3-mini"),
        visible_aliases_by_consumer={
            "my-farm-advisor": (
                DEFAULT_LOCAL_CANONICAL_ALIAS,
                "openrouter/anthropic/claude-3.5-sonnet",
                "opencode-go/o3-mini",
            ),
            "coder-kdense": ("opencode-go/o3-mini",),
        },
        default_alias_order=(
            DEFAULT_LOCAL_CANONICAL_ALIAS,
            "opencode-go/gpt-4.1-mini",
            "openrouter/anthropic/claude-3.5-sonnet",
            "opencode-go/o3-mini",
        ),
    )

    assert catalog.visible_aliases_for("my-farm-advisor") == (
        DEFAULT_LOCAL_CANONICAL_ALIAS,
        "openrouter/anthropic/claude-3.5-sonnet",
        "opencode-go/o3-mini",
    )
    assert catalog.fallback_alias_order_for("my-farm-advisor") == (
        DEFAULT_LOCAL_CANONICAL_ALIAS,
        "openrouter/anthropic/claude-3.5-sonnet",
        "opencode-go/o3-mini",
    )
    assert catalog.visible_aliases_for("coder-kdense") == ("opencode-go/o3-mini",)
    assert catalog.fallback_alias_order_for("coder-kdense") == ("opencode-go/o3-mini",)


def test_build_model_catalog_rejects_duplicate_aliases() -> None:
    with pytest.raises(ValueError, match="Duplicate model alias"):
        build_model_catalog(
            openrouter_model_ids=("anthropic/claude-3.5-sonnet",),
            nvidia_alias_targets={
                "openrouter/anthropic/claude-3.5-sonnet": "nvidia/moonshot/kimi-k2.5"
            },
        )
