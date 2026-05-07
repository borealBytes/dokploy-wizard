# pyright: reportMissingImports=false

from __future__ import annotations

import pytest

from dokploy_wizard.lifecycle import classify_modify_request
from dokploy_wizard.packs.catalog import (
    get_mutable_pack_env_keys,
    get_mutable_pack_resource_keys,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnershipLedger,
    RawEnvInput,
    resolve_desired_state,
)


def _raw(values: dict[str, str]) -> RawEnvInput:
    return RawEnvInput(format_version=1, values=values)


def _applied(completed_steps: tuple[str, ...]) -> AppliedStateCheckpoint:
    desired = resolve_desired_state(
        _raw(
            {
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
            }
        )
    )
    return AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=desired.fingerprint(),
        completed_steps=completed_steps,
    )


def test_modify_domain_change_starts_at_networking() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "true",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.net",
            "ENABLE_NEXTCLOUD": "true",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "nextcloud",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.start_phase == "networking"
    assert plan.initial_completed_steps == ("preflight", "dokploy_bootstrap")
    assert plan.phases_to_run == ("networking", "nextcloud")


def test_modify_cloudflare_auth_rotation_only_reruns_networking() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "CLOUDFLARE_API_TOKEN": "old-token",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "CLOUDFLARE_API_TOKEN": "new-token",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.start_phase == "networking"
    assert plan.phases_to_run == ("networking",)


def test_same_target_with_incomplete_checkpoint_and_stale_fingerprint_reruns_from_start() -> None:
    raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "CODER_WILDCARD_SUBDOMAIN": "*.coder",
        }
    )
    desired = resolve_desired_state(raw)

    plan = classify_modify_request(
        existing_raw=raw,
        existing_desired=desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="stale-fingerprint",
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=raw,
        requested_desired=desired,
    )

    assert plan.mode == "resume"
    assert plan.start_phase == "preflight"
    assert plan.preserved_phases == ()
    assert plan.initial_completed_steps == ()
    assert plan.phases_to_run == plan.applicable_phases


def test_modify_access_email_change_reruns_access_phase() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com,ops@example.com",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "openclaw",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.start_phase == "openclaw"
    assert plan.phases_to_run == ("openclaw", "cloudflare_access")


def test_modify_rejects_legacy_checkpoint_contract_even_when_steps_match_old_prefix() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)

    with pytest.raises(ValueError, match="lifecycle checkpoint contract version 1"):
        classify_modify_request(
            existing_raw=existing_raw,
            existing_desired=existing_desired,
            existing_applied=AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint=existing_desired.fingerprint(),
                completed_steps=(
                    "preflight",
                    "dokploy_bootstrap",
                    "networking",
                    "cloudflare_access",
                    "shared_core",
                    "openclaw",
                ),
                lifecycle_checkpoint_contract_version=1,
            ),
            existing_ledger=OwnershipLedger(format_version=1, resources=()),
            requested_raw=existing_raw,
            requested_desired=existing_desired,
        )


def test_modify_dokploy_admin_credential_change_reruns_nextcloud() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "true",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "ChangeMeSoon",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "true",
            "DOKPLOY_ADMIN_EMAIL": "clayton@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "ChangeMeSoon",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "nextcloud",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.start_phase == "nextcloud"
    assert plan.phases_to_run == ("nextcloud",)


def test_modify_rejects_stack_name_change() -> None:
    existing_raw = _raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"})
    requested_raw = _raw({"STACK_NAME": "other-stack", "ROOT_DOMAIN": "example.com"})

    with pytest.raises(ValueError, match="STACK_NAME changes are unsupported"):
        classify_modify_request(
            existing_raw=existing_raw,
            existing_desired=resolve_desired_state(existing_raw),
            existing_applied=AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint=resolve_desired_state(existing_raw).fingerprint(),
                completed_steps=(
                    "preflight",
                    "dokploy_bootstrap",
                    "networking",
                    "shared_core",
                ),
            ),
            existing_ledger=OwnershipLedger(format_version=1, resources=()),
            requested_raw=requested_raw,
            requested_desired=resolve_desired_state(requested_raw),
        )


def test_modify_disabling_nextcloud_is_supported_via_networking_only() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "true",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "false",
        }
    )

    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "nextcloud",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.start_phase == "networking"
    assert plan.phases_to_run == ("networking", "shared_core")
    assert plan.initial_completed_steps == ("preflight", "dokploy_bootstrap")


def test_modify_disabling_headscale_is_supported_via_networking_only() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_HEADSCALE": "true",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_HEADSCALE": "false",
        }
    )

    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "headscale",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.start_phase == "networking"
    assert plan.phases_to_run == ("networking",)


def test_modify_rejects_unmodeled_env_changes() -> None:
    existing_raw = _raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"})
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "HEADSCALE_ADMIN_EMAIL": "admin@example.com",
        }
    )

    with pytest.raises(ValueError, match="Unsupported mutable env keys"):
        classify_modify_request(
            existing_raw=existing_raw,
            existing_desired=resolve_desired_state(existing_raw),
            existing_applied=AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint=resolve_desired_state(existing_raw).fingerprint(),
                completed_steps=(
                    "preflight",
                    "dokploy_bootstrap",
                    "networking",
                    "shared_core",
                ),
            ),
            existing_ledger=OwnershipLedger(format_version=1, resources=()),
            requested_raw=requested_raw,
            requested_desired=resolve_desired_state(requested_raw),
        )


def test_modify_uses_explicit_pack_mutable_env_contract() -> None:
    assert get_mutable_pack_env_keys() == (
        "ADVISOR_GATEWAY_PASSWORD",
        "AI_DEFAULT_API_KEY",
        "AI_DEFAULT_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CF_ACCOUNT_ID",
        "DATA_MODE",
        "HERMES_INFERENCE_PROVIDER",
        "HERMES_MODEL",
        "MY_FARM_ADVISOR_CHANNELS",
        "MY_FARM_ADVISOR_FALLBACK_MODELS",
        "MY_FARM_ADVISOR_GATEWAY_PASSWORD",
        "MY_FARM_ADVISOR_NVIDIA_API_KEY",
        "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
        "MY_FARM_ADVISOR_PRIMARY_MODEL",
        "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
        "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID",
        "NEXTCLOUD_OPENCLAW_RESCAN_CRON",
        "NEXTCLOUD_OPENCLAW_RESCAN_TIMEZONE",
        "NVIDIA_BASE_URL",
        "OPENCLAW_BOOTSTRAP_REFRESH",
        "OPENCLAW_CHANNELS",
        "OPENCLAW_FALLBACK_MODELS",
        "OPENCLAW_FORCE_SKILL_SYNC",
        "OPENCLAW_GATEWAY_PASSWORD",
        "OPENCLAW_GATEWAY_TOKEN",
        "OPENCLAW_MEMORY_SEARCH_ENABLED",
        "OPENCLAW_NEXA_AGENT_DISPLAY_NAME",
        "OPENCLAW_NEXA_AGENT_EMAIL",
        "OPENCLAW_NEXA_AGENT_PASSWORD",
        "OPENCLAW_NEXA_AGENT_USER_ID",
        "OPENCLAW_NEXA_DEPLOYMENT_MODE",
        "OPENCLAW_NEXA_EDITOR_EVENTS_SHARED_SECRET",
        "OPENCLAW_NEXA_MEM0_API_KEY",
        "OPENCLAW_NEXA_MEM0_BASE_URL",
        "OPENCLAW_NEXA_MEM0_EMBEDDER_DIMENSIONS",
        "OPENCLAW_NEXA_MEM0_EMBEDDER_MODEL",
        "OPENCLAW_NEXA_MEM0_LLM_API_KEY",
        "OPENCLAW_NEXA_MEM0_LLM_BASE_URL",
        "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY",
        "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND",
        "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL",
        "OPENCLAW_NEXA_MEM0_VECTOR_DIMENSIONS",
        "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL",
        "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET",
        "OPENCLAW_NEXA_PRESENCE_POLICY",
        "OPENCLAW_NEXA_TALK_SHARED_SECRET",
        "OPENCLAW_NEXA_TALK_SIGNING_SECRET",
        "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD",
        "OPENCLAW_NEXA_WEBDAV_AUTH_USER",
        "OPENCLAW_NVIDIA_API_KEY",
        "OPENCLAW_OPENROUTER_API_KEY",
        "OPENCLAW_PRIMARY_MODEL",
        "OPENCLAW_SYNC_SKILLS_ON_START",
        "OPENCLAW_SYNC_SKILLS_OVERWRITE",
        "OPENCLAW_TELEGRAM_BOT_TOKEN",
        "OPENCLAW_TELEGRAM_GROUP_POLICY",
        "OPENCLAW_TELEGRAM_OWNER_USER_ID",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_GO_BASE_URL",
        "OUTBOUND_SMTP_FROM_ADDRESS",
        "OUTBOUND_SMTP_HOSTNAME",
        "R2_ACCESS_KEY_ID",
        "R2_BUCKET_NAME",
        "R2_ENDPOINT",
        "R2_SECRET_ACCESS_KEY",
        "TELEGRAM_ALLOWED_USERS",
        "TELEGRAM_DATA_PIPELINE_ALLOWED_USERS",
        "TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS",
        "TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE",
        "TELEGRAM_DATA_PIPELINE_BOT_TOKEN",
        "TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS",
        "TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE",
        "TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN",
        "TZ",
        "WORKSPACE_DATA_R2_PREFIX",
        "WORKSPACE_DATA_R2_RCLONE_MOUNT",
    )


def test_modify_runs_nextcloud_when_nexa_service_account_fields_change() -> None:
    existing_raw = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "true",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_NEXA_AGENT_USER_ID": "nexa-agent",
            "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa",
            "OPENCLAW_NEXA_AGENT_PASSWORD": "ChangeMeSoon",
        },
    )
    requested_raw = RawEnvInput(
        format_version=1,
        values={
            **existing_raw.values,
            "OPENCLAW_NEXA_AGENT_DISPLAY_NAME": "Nexa Runtime",
        },
    )

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=resolve_desired_state(existing_raw),
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=resolve_desired_state(existing_raw).fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "nextcloud",
                "openclaw",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=resolve_desired_state(requested_raw),
    )

    assert "nextcloud" in plan.phases_to_run
    assert "openclaw" in plan.phases_to_run


def test_modify_coder_hermes_env_change_reruns_coder() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "AI_DEFAULT_API_KEY": "old-key",
            "HERMES_INFERENCE_PROVIDER": "opencode-go",
            "HERMES_MODEL": "deepseek-v4-flash",
            "AI_DEFAULT_BASE_URL": "https://opencode.ai/zen/go/v1",
        }
    )
    requested_raw = _raw(
        {
            **existing_raw.values,
            "AI_DEFAULT_API_KEY": "new-key",
        }
    )

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=resolve_desired_state(existing_raw),
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=resolve_desired_state(existing_raw).fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "coder",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=resolve_desired_state(requested_raw),
    )

    assert plan.start_phase == "shared_core"
    assert plan.phases_to_run == ("shared_core", "coder")


def test_modify_uses_explicit_pack_mutable_resource_contract() -> None:
    assert get_mutable_pack_resource_keys() == (
        "MY_FARM_ADVISOR_REPLICAS",
        "OPENCLAW_REPLICAS",
    )


def test_modify_openclaw_replicas_change_uses_pack_mutable_resource_contract() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_REPLICAS": "1",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_REPLICAS": "3",
        }
    )

    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "openclaw",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "OPENCLAW_REPLICAS" in plan.reasons[0]
    assert plan.start_phase == "openclaw"
    assert plan.phases_to_run == ("openclaw",)


def test_modify_openclaw_channels_change_uses_pack_mutable_contract() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "matrix,telegram",
            "ENABLE_MATRIX": "true",
        }
    )

    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "openclaw",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "OPENCLAW_CHANNELS" in plan.reasons[0]
    assert plan.phases_to_run == ("networking", "shared_core", "matrix", "openclaw")


def test_modify_openclaw_gateway_token_change_uses_pack_mutable_contract() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_GATEWAY_TOKEN": "token-a",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_GATEWAY_TOKEN": "token-b",
        }
    )

    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "openclaw",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "OPENCLAW_GATEWAY_TOKEN" in plan.reasons[0]
    assert plan.start_phase == "openclaw"
    assert plan.phases_to_run == ("openclaw",)


def test_modify_openclaw_internal_hostname_reconciles_networking_and_openclaw_only() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_INTERNAL_SUBDOMAIN": "openclaw-internal",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_OPENCLAW": "true",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_INTERNAL_SUBDOMAIN": "agent-openclaw",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )

    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "openclaw",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "OPENCLAW_INTERNAL_SUBDOMAIN" in plan.reasons[0]
    assert plan.start_phase == "openclaw"
    assert plan.phases_to_run == ("openclaw",)
