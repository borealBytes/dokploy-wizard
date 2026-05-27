# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import replace

import pytest

from dokploy_wizard.core import build_shared_core_plan
from dokploy_wizard.core.planner import build_pack_env_specs
from dokploy_wizard.lifecycle import applicable_phases_for
from dokploy_wizard.packs.catalog import get_pack_definition, iter_pack_catalog
from dokploy_wizard.packs.env_metadata import (
    PackEnvMetadataError,
    get_pack_env_metadata,
    iter_pack_env_metadata,
    validate_pack_env_metadata,
)
from dokploy_wizard.packs.resolver import resolve_pack_selection
from dokploy_wizard.state import RawEnvInput, resolve_desired_state

_MY_FARM_ADVISOR_EXPECTED_MUTABLE_ENV_KEYS = {
    "ADVISOR_GATEWAY_PASSWORD",
    "MY_FARM_ADVISOR_GATEWAY_PASSWORD",
    "MY_FARM_ADVISOR_CHANNELS",
    "AI_DEFAULT_API_KEY",
    "AI_DEFAULT_BASE_URL",
    "AI_DEFAULT_PROVIDER",
    "AI_DEFAULT_MODEL",
    "ANTHROPIC_API_KEY",
    "LITELLM_OPENCODE_GO_API_KEY",
    "LITELLM_OPENROUTER_API_KEY",
    "LITELLM_OPENROUTER_MODELS",
    "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
    "MY_FARM_ADVISOR_NVIDIA_API_KEY",
    "NVIDIA_BASE_URL",
    "MY_FARM_ADVISOR_PRIMARY_MODEL",
    "MY_FARM_ADVISOR_FALLBACK_MODELS",
    "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
    "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID",
    "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
    "TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE",
    "TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS",
    "TELEGRAM_DATA_PIPELINE_BOT_TOKEN",
    "TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE",
    "TELEGRAM_DATA_PIPELINE_ALLOWED_USERS",
    "TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS",
    "TELEGRAM_ALLOWED_USERS",
    "OPENCLAW_TELEGRAM_GROUP_POLICY",
    "TZ",
    "OPENCLAW_SYNC_SKILLS_ON_START",
    "OPENCLAW_SYNC_SKILLS_OVERWRITE",
    "OPENCLAW_FORCE_SKILL_SYNC",
    "OPENCLAW_BOOTSTRAP_REFRESH",
    "OPENCLAW_MEMORY_SEARCH_ENABLED",
    "R2_BUCKET_NAME",
    "R2_ENDPOINT",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "CF_ACCOUNT_ID",
    "DATA_MODE",
    "WORKSPACE_DATA_R2_RCLONE_MOUNT",
    "WORKSPACE_DATA_R2_PREFIX",
}


def test_catalog_exposes_expected_pack_metadata() -> None:
    names = [pack.name for pack in iter_pack_catalog()]

    assert names == [
        "headscale",
        "matrix",
        "nextcloud",
        "moodle",
        "docuseal",
        "surfsense",
        "seaweedfs",
        "coder",
        "openclaw",
        "my-farm-advisor",
    ]
    assert get_pack_definition("headscale").default_enabled is False
    assert get_pack_definition("seaweedfs").slot is None
    assert get_pack_definition("seaweedfs").hostnames[0].key == "s3"
    assert get_pack_definition("coder").hostnames[1].key == "coder-wildcard"
    assert "AI_DEFAULT_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_BASE_URL" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_PROVIDER" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_MODEL" in get_pack_definition("coder").mutable_env_keys
    assert "HERMES_INFERENCE_PROVIDER" in get_pack_definition("coder").mutable_env_keys
    assert "HERMES_MODEL" in get_pack_definition("coder").mutable_env_keys
    assert "LITELLM_OPENCODE_GO_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "LITELLM_OPENROUTER_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "LITELLM_OPENROUTER_MODELS" in get_pack_definition("coder").mutable_env_keys
    assert "OPENCODE_GO_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "OPENCODE_GO_BASE_URL" in get_pack_definition("coder").mutable_env_keys
    assert get_pack_definition("openclaw").slot is None
    assert get_pack_definition("my-farm-advisor").slot is None
    assert get_pack_definition("openclaw").mutable_resource_keys == ("OPENCLAW_REPLICAS",)
    assert "OPENCLAW_NEXA_DEPLOYMENT_MODE" in get_pack_definition("openclaw").mutable_env_keys
    assert "OPENCLAW_NEXA_MEM0_BASE_URL" in get_pack_definition("openclaw").mutable_env_keys
    assert "OPENCLAW_NEXA_PRESENCE_POLICY" in get_pack_definition("openclaw").mutable_env_keys
    assert "OPENCLAW_NEXA_TALK_SHARED_SECRET" in get_pack_definition("openclaw").mutable_env_keys
    assert "AI_DEFAULT_PROVIDER" in get_pack_definition("openclaw").mutable_env_keys
    assert "AI_DEFAULT_MODEL" in get_pack_definition("openclaw").mutable_env_keys
    assert "LITELLM_OPENCODE_GO_API_KEY" in get_pack_definition("openclaw").mutable_env_keys
    assert "LITELLM_OPENROUTER_API_KEY" in get_pack_definition("openclaw").mutable_env_keys
    assert "LITELLM_OPENROUTER_MODELS" in get_pack_definition("openclaw").mutable_env_keys
    assert get_pack_definition("my-farm-advisor").depends_on == ()
    assert _MY_FARM_ADVISOR_EXPECTED_MUTABLE_ENV_KEYS <= set(
        get_pack_definition("my-farm-advisor").mutable_env_keys
    )
    assert get_pack_definition("my-farm-advisor").mutable_resource_keys == (
        "MY_FARM_ADVISOR_REPLICAS",
    )
    surfsense = get_pack_definition("surfsense")
    assert surfsense.env_flag == "ENABLE_SURFSENSE"
    assert surfsense.shared_core_requirements == ("postgres", "redis")
    assert [
        (hostname.key, hostname.default_subdomain, hostname.env_key)
        for hostname in surfsense.hostnames
    ] == [
        ("surfsense", "surfsense", "SURFSENSE_SUBDOMAIN"),
        ("surfsense-api", "surfsense-api", "SURFSENSE_API_SUBDOMAIN"),
        ("surfsense-zero", "surfsense-zero", "SURFSENSE_ZERO_SUBDOMAIN"),
    ]
    assert {
        "SURFSENSE_SUBDOMAIN",
        "SURFSENSE_API_SUBDOMAIN",
        "SURFSENSE_ZERO_SUBDOMAIN",
        "SURFSENSE_VERSION",
        "SURFSENSE_AUTH_TYPE",
        "SURFSENSE_ETL_SERVICE",
        "SURFSENSE_EMBEDDING_MODEL",
        "SURFSENSE_PRIMARY_MODEL",
        "SURFSENSE_FALLBACK_MODELS",
    } <= set(surfsense.mutable_env_keys)


def test_pack_env_metadata_covers_catalog_and_explicit_classifications() -> None:
    validate_pack_env_metadata()
    catalog_keys = {
        (pack.name, key) for pack in iter_pack_catalog() for key in pack.mutable_env_keys
    }
    metadata_keys = {(entry.pack_name, entry.env_key) for entry in iter_pack_env_metadata()}

    assert metadata_keys == catalog_keys

    openclaw_secret = get_pack_env_metadata("openclaw", "OPENCLAW_NEXA_TALK_SHARED_SECRET")
    assert openclaw_secret.sensitive is True
    assert openclaw_secret.required is False
    assert openclaw_secret.shared is False
    assert openclaw_secret.owner == "openclaw"
    assert openclaw_secret.target_service_suffixes == ("openclaw",)
    assert openclaw_secret.canonical_placeholder_name == (
        "OPENCLAW_OPENCLAW_NEXA_TALK_SHARED_SECRET"
    )

    openclaw_model = get_pack_env_metadata("openclaw", "OPENCLAW_PRIMARY_MODEL")
    assert openclaw_model.sensitive is False
    assert openclaw_model.required is False
    assert openclaw_model.canonical_placeholder_name == "OPENCLAW_OPENCLAW_PRIMARY_MODEL"

    shared_default = get_pack_env_metadata("my-farm-advisor", "AI_DEFAULT_API_KEY")
    assert shared_default.sensitive is True
    assert shared_default.shared is True
    assert shared_default.owner == "shared-ai-defaults"
    assert shared_default.canonical_placeholder_name == "AI_DEFAULT_API_KEY"

    surfsense_auth_type = get_pack_env_metadata("surfsense", "SURFSENSE_AUTH_TYPE")
    assert surfsense_auth_type.sensitive is False
    assert surfsense_auth_type.required is False
    assert surfsense_auth_type.shared is False
    assert surfsense_auth_type.owner == "surfsense"
    assert surfsense_auth_type.target_service_suffixes == (
        "surfsense-web",
        "surfsense-backend",
        "surfsense-zero",
    )
    assert surfsense_auth_type.canonical_placeholder_name == "SURFSENSE_SURFSENSE_AUTH_TYPE"

    surfsense_model = get_pack_env_metadata("surfsense", "SURFSENSE_PRIMARY_MODEL")
    assert surfsense_model.sensitive is False
    assert surfsense_model.canonical_placeholder_name == "SURFSENSE_SURFSENSE_PRIMARY_MODEL"


def test_pack_env_metadata_rejects_unrelated_non_shared_placeholder_collisions() -> None:
    entries = list(iter_pack_env_metadata())
    openclaw_gateway = get_pack_env_metadata("openclaw", "OPENCLAW_GATEWAY_PASSWORD")
    farm_gateway_index = entries.index(
        get_pack_env_metadata("my-farm-advisor", "MY_FARM_ADVISOR_GATEWAY_PASSWORD")
    )
    entries[farm_gateway_index] = replace(
        entries[farm_gateway_index],
        canonical_placeholder_name=openclaw_gateway.canonical_placeholder_name,
    )

    with pytest.raises(PackEnvMetadataError) as exc_info:
        validate_pack_env_metadata(entries)

    message = str(exc_info.value)
    assert "Non-shared pack env placeholder collision" in message
    assert "OPENCLAW_OPENCLAW_GATEWAY_PASSWORD" in message
    assert "my-farm-advisor" in message


def test_pack_env_specs_skip_empty_optional_values_without_required_placeholders() -> None:
    specs = build_pack_env_specs(
        "wizard-stack",
        ("openclaw",),
        {
            "OPENCLAW_TELEGRAM_OWNER_USER_ID": "",
            "OPENCLAW_PRIMARY_MODEL": "openrouter/model-a",
        },
    )

    specs_by_name = {spec.name: spec for spec in specs}
    assert "OPENCLAW_OPENCLAW_TELEGRAM_OWNER_USER_ID" not in specs_by_name
    model_spec = specs_by_name["OPENCLAW_OPENCLAW_PRIMARY_MODEL"]
    assert model_spec.sensitive is False
    assert model_spec.required is False
    assert model_spec.placeholder is None


def test_pack_env_specs_use_redaction_classification_and_target_services() -> None:
    specs = build_pack_env_specs(
        "wizard-stack",
        ("my-farm-advisor", "openclaw"),
        {
            "AI_DEFAULT_API_KEY": "SECRET_TEST_SHARED_AI_VALUE",
            "OPENCLAW_GATEWAY_PASSWORD": "SECRET_TEST_OPENCLAW_GATEWAY_VALUE",
            "OPENCLAW_PRIMARY_MODEL": "openrouter/model-a",
            "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN": "SECRET_TEST_FARM_BOT_TOKEN_VALUE",
        },
    )
    specs_by_name = {spec.name: spec for spec in specs}

    shared_ai = specs_by_name["AI_DEFAULT_API_KEY"]
    assert shared_ai.sensitive is True
    assert shared_ai.owner == "shared-ai-defaults"
    assert shared_ai.target_services == (
        "wizard-stack-openclaw",
        "wizard-stack-my-farm-advisor",
    )

    openclaw_gateway = specs_by_name["OPENCLAW_OPENCLAW_GATEWAY_PASSWORD"]
    assert openclaw_gateway.sensitive is True
    assert openclaw_gateway.owner == "openclaw"
    assert openclaw_gateway.target_services == ("wizard-stack-openclaw",)
    assert openclaw_gateway.placeholder == (
        "${OPENCLAW_OPENCLAW_GATEWAY_PASSWORD:?OPENCLAW_OPENCLAW_GATEWAY_PASSWORD is required}"
    )

    openclaw_model = specs_by_name["OPENCLAW_OPENCLAW_PRIMARY_MODEL"]
    assert openclaw_model.sensitive is False
    assert openclaw_model.target_services == ("wizard-stack-openclaw",)

    farm_bot = specs_by_name["MY_FARM_ADVISOR_MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN"]
    assert farm_bot.sensitive is True
    assert farm_bot.owner == "my-farm-advisor"
    assert farm_bot.target_services == ("wizard-stack-my-farm-advisor",)


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

    assert selection.enabled_packs == ("matrix", "my-farm-advisor", "openclaw")
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


def test_resolver_builds_surfsense_hostnames_from_packs_selection() -> None:
    selection = resolve_pack_selection(
        {
            "PACKS": "surfsense",
        },
        root_domain="example.com",
    )

    assert selection.selected_packs == ("surfsense",)
    assert selection.enabled_packs == ("surfsense",)
    assert selection.hostnames == {
        "surfsense": "surfsense.example.com",
        "surfsense-api": "surfsense-api.example.com",
        "surfsense-zero": "surfsense-zero.example.com",
    }


def test_resolver_full_pack_list_with_surfsense_has_no_hostname_collisions() -> None:
    selection = resolve_pack_selection(
        {
            "PACKS": "nextcloud,openclaw,my-farm-advisor,seaweedfs,coder,docuseal,surfsense",
        },
        root_domain="example.com",
    )

    assert selection.selected_packs == (
        "coder",
        "docuseal",
        "my-farm-advisor",
        "nextcloud",
        "openclaw",
        "seaweedfs",
        "surfsense",
    )
    assert selection.enabled_packs == selection.selected_packs
    assert selection.hostnames["surfsense"] == "surfsense.example.com"
    assert selection.hostnames["surfsense-api"] == "surfsense-api.example.com"
    assert selection.hostnames["surfsense-zero"] == "surfsense-zero.example.com"
    assert len(set(selection.hostnames.values())) == len(selection.hostnames)


def test_full_pack_lifecycle_schedules_surfsense_after_faster_application_packs() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "nextcloud,openclaw,my-farm-advisor,seaweedfs,coder,docuseal,surfsense",
                "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "sk-test-placeholder",
                "SEAWEEDFS_ACCESS_KEY": "seaweed-access-key",
                "SEAWEEDFS_SECRET_KEY": "seaweed-secret-key",
            },
        )
    )

    phases = applicable_phases_for(desired_state)

    assert phases == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "seaweedfs",
        "nextcloud",
        "docuseal",
        "coder",
        "openclaw",
        "my-farm-advisor",
        "cloudflare_access",
        "surfsense",
    )
    for earlier_pack in (
        "seaweedfs",
        "nextcloud",
        "docuseal",
        "coder",
        "openclaw",
        "my-farm-advisor",
    ):
        assert phases.index(earlier_pack) < phases.index("surfsense")


def test_resolved_state_and_shared_core_use_catalog_requirements() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "catalog-stack",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "matrix,my-farm-advisor",
                "MY_FARM_ADVISOR_OPENROUTER_API_KEY": "sk-test-placeholder",
            },
        )
    )

    assert desired_state.selected_packs == ("matrix", "my-farm-advisor")
    assert desired_state.enabled_packs == ("matrix", "my-farm-advisor")
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


def test_resolved_state_includes_surfsense_shared_core_allocations() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "surfsense-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_SURFSENSE": "true",
            },
        )
    )

    assert desired_state.enabled_packs == ("surfsense",)
    assert desired_state.hostnames["surfsense"] == "surfsense.example.com"
    assert desired_state.hostnames["surfsense-api"] == "surfsense-api.example.com"
    assert desired_state.hostnames["surfsense-zero"] == "surfsense-zero.example.com"
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "surfsense"
    ]
    allocation = desired_state.shared_core.allocations[0]
    assert allocation.postgres is not None
    assert allocation.redis is not None
