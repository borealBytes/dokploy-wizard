# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import replace

import pytest

from dokploy_wizard.lifecycle import (
    LifecyclePlan,
    applicable_phases_for,
    classify_modify_request,
    force_phase_into_plan,
)
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
            "ENABLE_CODER": "true",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
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
                "coder",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.start_phase == "cloudflare_access"
    assert plan.phases_to_run == ("cloudflare_access",)


def test_modify_disabling_cloudflare_access_is_disable_only() -> None:
    # Dropping the recipient list is the supported opt-out: no phase may re-run
    # (the Access phase is no longer applicable), but the plan must classify
    # cleanly so the disable plan can tear the existing records down.
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
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
                "coder",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.phases_to_run == ()
    assert "cloudflare_access" not in plan.applicable_phases


def test_root_domain_rotation_reapplies_cloudflare_access_when_enabled() -> None:
    # A domain rotation rewrites the derived admin hostname; with Access opted
    # in, the phase has to re-run or the policy keeps pointing at the old domain.
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "old.example.com",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "new.example.com",
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
            completed_steps=applicable_phases_for(existing_desired),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert "networking" in plan.phases_to_run
    assert "cloudflare_access" in plan.phases_to_run


def test_modify_rejects_legacy_checkpoint_contract_even_when_steps_match_old_prefix() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
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
                    "coder",
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
            "DOKPLOY_ADMIN_EMAIL": "adminuser@example.com",
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


def test_modify_ignores_ephemeral_docker_auth_key_differences() -> None:
    existing_raw = _raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"})
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "DOCKER_USERNAME": "operator",
            "DOCKER_PAT": "temporary-token",
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

    assert plan.mode == "noop"
    assert plan.phases_to_run == ()
    assert plan.start_phase is None
    assert plan.raw_equivalent is True
    assert plan.desired_equivalent is True


def test_modify_ignores_remote_helper_key_differences() -> None:
    existing_raw = _raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"})
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "VPS_HOST": "203.0.113.10",
            "VPS_ROOT_PASSWORD": "temporary-root-password",
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

    assert plan.mode == "noop"
    assert plan.phases_to_run == ()
    assert plan.start_phase is None
    assert plan.raw_equivalent is True
    assert plan.desired_equivalent is True


def test_modify_ignores_desired_only_dokploy_api_url_drift() -> None:
    raw = _raw({"STACK_NAME": "wizard-stack", "ROOT_DOMAIN": "example.com"})
    requested_desired = resolve_desired_state(raw)
    existing_desired = replace(
        requested_desired,
        dokploy_api_url="https://dokploy.previous.example.test/api",
    )
    requested_desired = replace(
        requested_desired,
        dokploy_api_url="https://dokploy.current.example.test/api",
    )

    plan = classify_modify_request(
        existing_raw=raw,
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
        requested_raw=raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "noop"
    assert plan.phases_to_run == ()
    assert plan.start_phase is None
    assert plan.raw_equivalent is True
    assert plan.desired_equivalent is True


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


def test_modify_ignores_redundant_pack_enable_flags_when_packs_is_authoritative() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder",
            "ENABLE_CODER": "true",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder",
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
                "coder",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert existing_desired.enabled_packs == requested_desired.enabled_packs == ("coder",)
    assert plan.mode == "noop"
    assert plan.phases_to_run == ()


def test_modify_uses_explicit_pack_mutable_env_contract() -> None:
    assert get_mutable_pack_env_keys() == (
        "AI_DEFAULT_API_KEY",
        "AI_DEFAULT_BASE_URL",
        "AI_DEFAULT_MODEL",
        "AI_DEFAULT_MODEL_PATTERN",
        "AI_DEFAULT_PROVIDER",
        "HERMES_DESKTOP_CHANNEL",
        "HERMES_INFERENCE_PROVIDER",
        "HERMES_MODEL",
        "LITELLM_NVIDIA_API_KEY",
        "LITELLM_NVIDIA_BASE_URL",
        "LITELLM_NVIDIA_MODELS",
        "LITELLM_OPENCODE_GO_API_KEY",
        "LITELLM_OPENROUTER_API_KEY",
        "LITELLM_OPENROUTER_MODELS",
        "NVIDIA_BASE_URL",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_GO_BASE_URL",
        "OUTBOUND_SMTP_FROM_ADDRESS",
        "OUTBOUND_SMTP_HOSTNAME",
    )


def test_modify_accepts_canonical_nvidia_litellm_key_rotation_for_consumers() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder",
            "AI_DEFAULT_PROVIDER": "nvidia",
            "AI_DEFAULT_MODEL": "moonshotai/kimi-k2.5",
            "LITELLM_NVIDIA_API_KEY": "old-fake-key",
            "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia.example.com/v1",
            "LITELLM_NVIDIA_MODELS": "moonshotai/kimi-k2.6",
        }
    )
    requested_raw = _raw(
        {
            **existing_raw.values,
            "LITELLM_NVIDIA_API_KEY": "new-fake-key",
            "LITELLM_NVIDIA_BASE_URL": "https://integrate.api.nvidia-new.example.com/v1",
            "LITELLM_NVIDIA_MODELS": (
                "moonshotai/kimi-k2.6,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
            ),
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
                "coder",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.phases_to_run == ("shared_core", "coder")
    assert plan.initial_completed_steps == ("preflight", "dokploy_bootstrap", "networking")


def test_modify_coder_hermes_env_change_reruns_coder() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "AI_DEFAULT_API_KEY": "old-key",
            "HERMES_INFERENCE_PROVIDER": "opencode-go",
            "HERMES_MODEL": "deepseek-v4.1-flash",
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


def test_modify_coder_opencode_go_key_rotation_reruns_shared_core_and_coder() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "OPENCODE_GO_API_KEY": "token-a",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "OPENCODE_GO_API_KEY": "token-b",
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
                "coder",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "OPENCODE_GO_API_KEY" in plan.reasons[0]
    assert plan.start_phase == "shared_core"
    assert plan.phases_to_run == ("shared_core", "coder")


def _classify_mail_pack_modify(
    *,
    key: str,
    old_value: str,
    new_value: str,
) -> LifecyclePlan:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "docuseal,moodle",
            key: old_value,
        }
    )
    requested_raw = _raw(
        {
            **existing_raw.values,
            key: new_value,
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)

    return classify_modify_request(
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
                "moodle",
                "docuseal",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )


@pytest.mark.parametrize(
    ("key", "old_value", "new_value"),
    (
        ("OUTBOUND_SMTP_HOSTNAME", "mail.example.com", "smtp.example.com"),
        (
            "OUTBOUND_SMTP_FROM_ADDRESS",
            "DoNotReply@example.com",
            "NoReply@example.com",
        ),
    ),
)
def test_modify_outbound_mail_key_change_reruns_mail_relay_and_consumers(
    key: str, old_value: str, new_value: str
) -> None:
    plan = _classify_mail_pack_modify(key=key, old_value=old_value, new_value=new_value)

    assert plan.mode == "modify"
    assert plan.start_phase == "shared_core"
    assert plan.phases_to_run == ("shared_core", "moodle", "docuseal")


def test_modify_enabling_matrix_adds_networking_shared_core_and_matrix_phases() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "nextcloud",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "matrix,nextcloud",
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
    assert "PACKS" in plan.reasons[0]
    assert plan.phases_to_run == ("networking", "shared_core", "matrix")
    assert "nextcloud" in plan.preserved_phases


def test_modify_litellm_desired_only_local_alias_removal_reruns_consumers() -> None:
    raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder,nextcloud",
            "AI_DEFAULT_PROVIDER": "openrouter",
            "AI_DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash:free",
            "LITELLM_OPENROUTER_API_KEY": "sk-test-openrouter",
            "LITELLM_OPENROUTER_MODELS": "deepseek/deepseek-v4.1-flash:free",
        }
    )
    requested_desired = resolve_desired_state(raw)
    assert requested_desired.shared_core.litellm is not None
    assert requested_desired.shared_core.litellm.default_model_alias_order == (
        "openrouter/deepseek/deepseek-v4.1-flash:free",
    )

    legacy_litellm = replace(
        requested_desired.shared_core.litellm,
        default_model_alias_order=(
            "local-model.internal/unsloth-active",
            *requested_desired.shared_core.litellm.default_model_alias_order,
        ),
    )
    existing_desired = replace(
        requested_desired,
        shared_core=replace(requested_desired.shared_core, litellm=legacy_litellm),
    )

    plan = classify_modify_request(
        existing_raw=raw,
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
                "coder",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.start_phase == "shared_core"
    assert plan.raw_equivalent is True
    assert plan.desired_equivalent is False
    assert plan.phases_to_run == ("shared_core", "coder")


def test_modify_rejects_arbitrary_desired_only_shared_core_drift() -> None:
    raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder",
        }
    )
    requested_desired = resolve_desired_state(raw)
    existing_desired = replace(
        requested_desired,
        shared_core=replace(
            requested_desired.shared_core,
            network_name="legacy-shared-network",
        ),
    )

    with pytest.raises(ValueError, match="not modeled as supported runtime mutations"):
        classify_modify_request(
            existing_raw=raw,
            existing_desired=existing_desired,
            existing_applied=AppliedStateCheckpoint(
                format_version=1,
                desired_state_fingerprint=existing_desired.fingerprint(),
                completed_steps=(
                    "preflight",
                    "dokploy_bootstrap",
                    "networking",
                    "shared_core",
                    "coder",
                ),
            ),
            existing_ledger=OwnershipLedger(format_version=1, resources=()),
            requested_raw=raw,
            requested_desired=requested_desired,
        )


def test_modify_uses_explicit_pack_mutable_resource_contract() -> None:
    # No surviving pack exposes mutable replica knobs, so the resource-key contract
    # is empty and any *_REPLICAS key stays an unsupported modify input.
    assert get_mutable_pack_resource_keys() == ()


def test_modify_litellm_admin_hostname_reconciles_networking_and_shared_core() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder",
            "LITELLM_ADMIN_SUBDOMAIN": "ai-gateway",
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
                "seaweedfs",
                "coder",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "LITELLM_ADMIN_SUBDOMAIN" in plan.reasons[0]
    # The admin hostname is not in desired hostnames, so the key alone has to reach
    # both the phase that publishes its DNS record and the service that advertises it.
    assert plan.phases_to_run == ("networking", "shared_core")
    assert plan.start_phase == "networking"


def test_modify_litellm_admin_hostname_reapplies_cloudflare_access_when_enabled() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
            "LITELLM_ADMIN_SUBDOMAIN": "litellm",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_CODER": "true",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
            "LITELLM_ADMIN_SUBDOMAIN": "ai-gateway",
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
                "coder",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "cloudflare_access" in plan.phases_to_run
    assert plan.phases_to_run == ("networking", "shared_core", "cloudflare_access")


def test_modify_nextcloud_hostname_change_reconciles_networking_and_nextcloud_only() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "true",
            "NEXTCLOUD_SUBDOMAIN": "cloud",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_NEXTCLOUD": "true",
            "NEXTCLOUD_SUBDOMAIN": "files",
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
                "nextcloud",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "NEXTCLOUD_SUBDOMAIN" in plan.reasons[0]
    assert plan.start_phase == "networking"
    assert plan.phases_to_run == ("networking", "nextcloud")


def test_modify_schedules_shared_core_for_projection_drift_and_unfinished_tail() -> None:
    raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,nextcloud",
            "LITELLM_OPENROUTER_API_KEY": "or-key",
        }
    )
    requested_desired = resolve_desired_state(raw)
    assert requested_desired.shared_core.model_sync is not None

    # Simulates a wizard upgrade that added the model-sync projection while
    # the operator env stayed identical: the persisted desired state lacks
    # model_sync, and the previous install stopped before coder applied.
    existing_desired = replace(
        requested_desired,
        shared_core=replace(requested_desired.shared_core, model_sync=None),
    )
    applicable = applicable_phases_for(requested_desired)
    assert applicable[-1] == "coder"
    completed = tuple(phase for phase in applicable if phase != "coder")
    existing_applied = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=existing_desired.fingerprint(),
        completed_steps=completed,
    )

    plan = classify_modify_request(
        existing_raw=raw,
        existing_desired=existing_desired,
        existing_applied=existing_applied,
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.phases_to_run == ("shared_core", "coder")
    assert "nextcloud" in plan.preserved_phases


def _noop_plan(applicable: tuple[str, ...]) -> LifecyclePlan:
    return LifecyclePlan(
        mode="noop",
        reasons=("Requested raw input and desired state match the persisted target.",),
        applicable_phases=applicable,
        phases_to_run=(),
        preserved_phases=applicable,
        initial_completed_steps=applicable,
        start_phase=None,
        raw_equivalent=True,
        desired_equivalent=True,
    )


_FULL_APPLICABLE = (
    "preflight",
    "dokploy_bootstrap",
    "networking",
    "shared_core",
    "coder",
    "cloudflare_access",
)


def test_force_phase_promotes_noop_plan_into_single_shared_core_pass() -> None:
    plan = force_phase_into_plan(
        _noop_plan(_FULL_APPLICABLE),
        phase="shared_core",
        reason="upgrade repinned the shared-core image",
    )

    assert plan.mode == "modify"
    assert plan.phases_to_run == ("shared_core",)
    assert plan.start_phase == "shared_core"
    assert set(plan.preserved_phases) == set(_FULL_APPLICABLE) - {"shared_core"}
    assert plan.initial_completed_steps == _FULL_APPLICABLE
    assert plan.reasons == ("upgrade repinned the shared-core image",)


def test_force_phase_folds_into_existing_modify_plan_without_duplicating() -> None:
    existing = LifecyclePlan(
        mode="modify",
        reasons=("docuseal SMTP moved",),
        applicable_phases=_FULL_APPLICABLE,
        phases_to_run=("coder",),
        preserved_phases=(
            "preflight",
            "dokploy_bootstrap",
            "networking",
            "shared_core",
            "cloudflare_access",
        ),
        initial_completed_steps=("preflight", "dokploy_bootstrap", "networking"),
        start_phase="coder",
        raw_equivalent=False,
        desired_equivalent=False,
    )

    plan = force_phase_into_plan(existing, phase="shared_core", reason="forced")

    assert plan.mode == "modify"
    assert plan.phases_to_run == ("shared_core", "coder")
    assert "shared_core" not in plan.preserved_phases
    assert plan.reasons == ("docuseal SMTP moved", "forced")
    # Forcing an already-scheduled phase is a no-op.
    assert force_phase_into_plan(plan, phase="shared_core", reason="forced") is plan


def test_force_phase_ignores_phases_outside_applicable_set() -> None:
    noop = _noop_plan(("preflight", "dokploy_bootstrap", "networking", "shared_core"))

    assert force_phase_into_plan(noop, phase="coder", reason="x") is noop
