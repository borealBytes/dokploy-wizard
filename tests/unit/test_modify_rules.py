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
    assert plan.phases_to_run == ("networking",)
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
        "OPENCLAW_CHANNELS",
        "OPENCLAW_FALLBACK_MODELS",
        "OPENCLAW_GATEWAY_PASSWORD",
        "OPENCLAW_GATEWAY_TOKEN",
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
        "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET",
        "OPENCLAW_NEXA_PRESENCE_POLICY",
        "OPENCLAW_NEXA_TALK_SHARED_SECRET",
        "OPENCLAW_NEXA_TALK_SIGNING_SECRET",
        "OPENCLAW_NVIDIA_API_KEY",
        "OPENCLAW_OPENROUTER_API_KEY",
        "OPENCLAW_PRIMARY_MODEL",
        "OPENCLAW_TELEGRAM_BOT_TOKEN",
        "OPENCLAW_TELEGRAM_OWNER_USER_ID",
    )


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
