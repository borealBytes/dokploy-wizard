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
from dokploy_wizard.state.models import StateValidationError


def test_catalog_exposes_expected_pack_metadata() -> None:
    names = [pack.name for pack in iter_pack_catalog()]

    assert names == [
        "headscale",
        "matrix",
        "nextcloud",
        "moodle",
        "docuseal",
        "seaweedfs",
        "filex",
        "gitea",
        "coder",
    ]
    assert get_pack_definition("headscale").default_enabled is False
    assert get_pack_definition("seaweedfs").slot is None
    # Gitea is canonically selected through a PACKS entry; its legacy env flag
    # lives one release longer as a deprecated alias (the resolver stamps the
    # note). Exactly one pack may wear the marker — it is a migration aid, not
    # a permanent second selection path.
    assert get_pack_definition("gitea").env_flag_deprecated is True
    assert [pack.name for pack in iter_pack_catalog() if pack.env_flag_deprecated] == ["gitea"]
    # The S3 endpoint is internal-only: the pack models no public hostname.
    assert get_pack_definition("seaweedfs").hostnames == ()
    assert get_pack_definition("filex").depends_on == ("seaweedfs",)
    assert get_pack_definition("filex").hostnames[0].key == "filex"
    assert get_pack_definition("nextcloud").mutable_env_keys == ()
    # The slot system is gone: no surviving pack occupies a mutual-exclusion slot.
    assert all(pack.slot is None for pack in iter_pack_catalog())
    assert get_pack_definition("coder").hostnames[1].key == "coder-wildcard"
    assert "AI_DEFAULT_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_BASE_URL" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_PROVIDER" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_MODEL" in get_pack_definition("coder").mutable_env_keys
    assert "AI_DEFAULT_MODEL_PATTERN" in get_pack_definition("coder").mutable_env_keys
    assert "HERMES_INFERENCE_PROVIDER" in get_pack_definition("coder").mutable_env_keys
    assert "HERMES_MODEL" in get_pack_definition("coder").mutable_env_keys
    assert "HERMES_DESKTOP_CHANNEL" in get_pack_definition("coder").mutable_env_keys
    assert "LITELLM_OPENCODE_GO_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "LITELLM_OPENROUTER_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "LITELLM_OPENROUTER_MODELS" in get_pack_definition("coder").mutable_env_keys
    assert "OPENCODE_GO_API_KEY" in get_pack_definition("coder").mutable_env_keys
    assert "OPENCODE_GO_BASE_URL" in get_pack_definition("coder").mutable_env_keys
    assert get_pack_definition("coder").mutable_resource_keys == ()
    assert get_pack_definition("moodle").depends_on == ()
    assert get_pack_definition("moodle").mutable_env_keys == (
        "OUTBOUND_SMTP_HOSTNAME",
        "OUTBOUND_SMTP_FROM_ADDRESS",
    )
    assert get_pack_definition("docuseal").mutable_env_keys == (
        "OUTBOUND_SMTP_HOSTNAME",
        "OUTBOUND_SMTP_FROM_ADDRESS",
    )


def test_pack_env_metadata_covers_catalog_and_explicit_classifications() -> None:
    validate_pack_env_metadata()
    catalog_keys = {
        (pack.name, key) for pack in iter_pack_catalog() for key in pack.mutable_env_keys
    }
    metadata_keys = {(entry.pack_name, entry.env_key) for entry in iter_pack_env_metadata()}

    assert metadata_keys == catalog_keys

    shared_default = get_pack_env_metadata("coder", "AI_DEFAULT_API_KEY")
    assert shared_default.sensitive is True
    assert shared_default.required is False
    assert shared_default.shared is True
    assert shared_default.owner == "shared-ai-defaults"
    assert shared_default.canonical_placeholder_name == "AI_DEFAULT_API_KEY"

    coder_model = get_pack_env_metadata("coder", "HERMES_MODEL")
    assert coder_model.sensitive is False
    assert coder_model.required is False
    assert coder_model.canonical_placeholder_name == "CODER_HERMES_MODEL"

    coder_litellm_key = get_pack_env_metadata("coder", "LITELLM_OPENROUTER_API_KEY")
    assert coder_litellm_key.sensitive is True
    assert coder_litellm_key.shared is True
    assert coder_litellm_key.owner == "shared-litellm"
    assert coder_litellm_key.target_service_suffixes == ("shared-litellm",)
    assert coder_litellm_key.canonical_placeholder_name == "LITELLM_OPENROUTER_API_KEY"

    mail_hostname = get_pack_env_metadata("moodle", "OUTBOUND_SMTP_HOSTNAME")
    assert mail_hostname.sensitive is False
    assert mail_hostname.required is False
    assert mail_hostname.shared is True
    assert mail_hostname.owner == "shared-mail-relay"
    assert mail_hostname.target_service_suffixes == ("shared-postfix",)
    assert mail_hostname.canonical_placeholder_name == "OUTBOUND_SMTP_HOSTNAME"


def test_pack_env_metadata_rejects_unrelated_non_shared_placeholder_collisions() -> None:
    entries = list(iter_pack_env_metadata())
    coder_model = get_pack_env_metadata("coder", "HERMES_MODEL")
    mail_hostname_index = entries.index(
        get_pack_env_metadata("moodle", "OUTBOUND_SMTP_HOSTNAME")
    )
    entries[mail_hostname_index] = replace(
        entries[mail_hostname_index],
        canonical_placeholder_name=coder_model.canonical_placeholder_name,
    )

    with pytest.raises(PackEnvMetadataError) as exc_info:
        validate_pack_env_metadata(entries)

    message = str(exc_info.value)
    assert "Non-shared pack env placeholder collision" in message
    assert coder_model.canonical_placeholder_name in message
    assert "shared-mail-relay" in message
    assert "OUTBOUND_SMTP_HOSTNAME" in message


def test_pack_env_specs_skip_empty_optional_values_without_required_placeholders() -> None:
    specs = build_pack_env_specs(
        "wizard-stack",
        ("coder",),
        {
            "OPENCODE_GO_BASE_URL": "",
            "HERMES_MODEL": "deepseek-v4.1-flash",
        },
    )

    specs_by_name = {spec.name: spec for spec in specs}
    assert "CODER_OPENCODE_GO_BASE_URL" not in specs_by_name
    model_spec = specs_by_name["CODER_HERMES_MODEL"]
    assert model_spec.sensitive is False
    assert model_spec.required is False
    assert model_spec.placeholder is None


def test_pack_env_specs_use_redaction_classification_and_target_services() -> None:
    specs = build_pack_env_specs(
        "wizard-stack",
        ("coder", "docuseal"),
        {
            "AI_DEFAULT_API_KEY": "SECRET_TEST_SHARED_AI_VALUE",
            "OPENCODE_GO_API_KEY": "SECRET_TEST_OPENCODE_GO_VALUE",
            "LITELLM_OPENROUTER_API_KEY": "SECRET_TEST_OPENROUTER_VALUE",
            "OUTBOUND_SMTP_HOSTNAME": "mail.example.com",
        },
    )
    specs_by_name = {spec.name: spec for spec in specs}

    shared_ai = specs_by_name["AI_DEFAULT_API_KEY"]
    assert shared_ai.sensitive is True
    assert shared_ai.owner == "shared-ai-defaults"
    assert shared_ai.target_services == ("wizard-stack-coder",)

    opencode_go = specs_by_name["CODER_OPENCODE_GO_API_KEY"]
    assert opencode_go.sensitive is True
    assert opencode_go.owner == "coder"
    assert opencode_go.target_services == ("wizard-stack-coder",)
    assert opencode_go.placeholder is None
    assert opencode_go.required is False

    litellm_key = specs_by_name["LITELLM_OPENROUTER_API_KEY"]
    assert litellm_key.sensitive is True
    assert litellm_key.owner == "shared-litellm"
    assert litellm_key.target_services == ("wizard-stack-shared-litellm",)

    mail_hostname = specs_by_name["OUTBOUND_SMTP_HOSTNAME"]
    assert mail_hostname.sensitive is False
    assert mail_hostname.owner == "shared-mail-relay"
    assert mail_hostname.target_services == ("wizard-stack-shared-postfix",)


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


def test_resolver_allows_mail_and_matrix_packs_together() -> None:
    selection = resolve_pack_selection(
        {
            "ENABLE_MOODLE": "true",
            "ENABLE_DOCUSEAL": "true",
            "ENABLE_MATRIX": "true",
        },
        root_domain="example.com",
    )

    assert selection.enabled_packs == ("docuseal", "matrix", "moodle")
    assert selection.enabled_features == ("dokploy",)


def test_resolver_rejects_unknown_pack_selection() -> None:
    with pytest.raises(
        StateValidationError,
        match=r"Unknown pack selection\(s\) in PACKS: \['does-not-exist'\]\.",
    ):
        resolve_pack_selection(
            {
                "PACKS": "does-not-exist",
            },
            root_domain="example.com",
        )


def test_resolver_explicit_disable_flag_overrides_packs_selection() -> None:
    selection = resolve_pack_selection(
        {
            "PACKS": "matrix",
            "ENABLE_MATRIX": "false",
        },
        root_domain="example.com",
    )

    assert selection.selected_packs == ()
    assert selection.enabled_packs == ()


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
        "coder-wildcard": "*.example.com",
    }


def test_resolver_preserves_explicit_nested_coder_wildcard_opt_in() -> None:
    selection = resolve_pack_selection(
        {
            "ENABLE_CODER": "true",
            "CODER_WILDCARD_SUBDOMAIN": "*.coder",
        },
        root_domain="example.com",
    )

    assert selection.enabled_packs == ("coder",)
    assert selection.hostnames == {
        "coder": "coder.example.com",
        "coder-wildcard": "*.coder.example.com",
    }


def test_resolver_full_pack_list_has_no_hostname_collisions() -> None:
    selection = resolve_pack_selection(
        {
            "PACKS": "nextcloud,seaweedfs,filex,gitea,coder,docuseal,matrix",
        },
        root_domain="example.com",
    )

    assert selection.selected_packs == (
        "coder",
        "docuseal",
        "filex",
        "gitea",
        "matrix",
        "nextcloud",
        "seaweedfs",
    )
    assert selection.enabled_packs == selection.selected_packs
    assert selection.hostnames == {
        "coder": "coder.example.com",
        "coder-wildcard": "*.example.com",
        "docuseal": "docuseal.example.com",
        "filex": "filex.example.com",
        "gitea": "gitea.example.com",
        "matrix": "matrix.example.com",
        "nextcloud": "nextcloud.example.com",
        "onlyoffice": "office.example.com",
    }
    assert len(set(selection.hostnames.values())) == len(selection.hostnames)


def test_full_pack_lifecycle_orders_application_packs_after_shared_core() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "PACKS": "nextcloud,seaweedfs,coder,docuseal,matrix,gitea",
            },
        )
    )

    assert desired_state.seaweedfs_access_key is None
    assert desired_state.seaweedfs_secret_key is None

    phases = applicable_phases_for(desired_state)

    assert phases == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "seaweedfs",
        "matrix",
        "nextcloud",
        "docuseal",
        "gitea",
        "coder",
    )
    for application_pack in (
        "seaweedfs",
        "matrix",
        "nextcloud",
        "docuseal",
        "gitea",
        "coder",
    ):
        assert phases.index("shared_core") < phases.index(application_pack)


def test_resolved_state_and_shared_core_use_catalog_requirements() -> None:
    values = {
        "STACK_NAME": "catalog-stack",
        "ROOT_DOMAIN": "example.com",
        "PACKS": "matrix,nextcloud",
    }
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values=values,
        )
    )

    assert desired_state.selected_packs == ("matrix", "nextcloud")
    assert desired_state.enabled_packs == ("matrix", "nextcloud")
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "matrix",
        "nextcloud",
    ]
    assert (
        build_shared_core_plan("catalog-stack", desired_state.enabled_packs, values=values)
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
    assert desired_state.hostnames["coder-wildcard"] == "*.example.com"
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "coder"
    ]


def test_resolved_state_includes_moodle_shared_core_allocation() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "moodle-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MOODLE": "true",
            },
        )
    )

    assert desired_state.enabled_packs == ("moodle",)
    assert desired_state.hostnames["moodle"] == "moodle.example.com"
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "moodle"
    ]
    allocation = desired_state.shared_core.allocations[0]
    assert allocation.postgres is not None
    assert allocation.redis is None
