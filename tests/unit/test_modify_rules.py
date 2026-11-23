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
            "ENABLE_MOODLE": "true",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.net",
            "ENABLE_MOODLE": "true",
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
                "moodle",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert plan.start_phase == "networking"
    assert plan.initial_completed_steps == ("preflight", "dokploy_bootstrap")
    assert plan.phases_to_run == ("networking", "shared_core", "moodle")


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


def test_modify_dokploy_admin_credential_change_reruns_pack_consumers() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "gitea,coder",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "ChangeMeSoon",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "gitea,coder",
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
                "seaweedfs",
                "gitea",
                "coder",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.start_phase == "gitea"
    assert plan.phases_to_run == ("gitea", "coder")


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


def test_modify_disabling_matrix_is_supported_via_networking_only() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_MATRIX": "true",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_MATRIX": "false",
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
                "matrix",
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
            "PACKS": "moodle",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "matrix,moodle",
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
                "moodle",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "PACKS" in plan.reasons[0]
    assert plan.phases_to_run == ("networking", "shared_core", "matrix")
    assert "moodle" in plan.preserved_phases


def test_modify_enabling_gitea_schedules_shared_core_and_gitea_phases() -> None:
    """Enabling the gitea pack via modify must live-provision its database.

    The shared-core Postgres init scripts only answer a first boot, so the
    per-pack role for a newly enabled pack is created by the shared_core
    phase's live-allocation path. A plan that runs 'gitea' without
    'shared_core' leaves the app crash-looping on password auth — the exact
    failure the first live proof measured.
    """
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,docuseal,moodle,filex",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,docuseal,moodle,filex,gitea",
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
            completed_steps=tuple(
                phase
                for phase in applicable_phases_for(existing_desired)
                if phase != "cloudflare_access"
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert set(plan.phases_to_run) >= {"shared_core", "gitea"}
    assert "filex" in plan.preserved_phases


def test_modify_gitea_flag_to_packs_alias_flip_is_noop() -> None:
    """An env-form flip from the deprecated flag to a PACKS entry is a noop.

    The box proof for the Gitea conversion relies on this: the applied state
    was installed with ENABLE_GITEA=true, and re-running modify with the same
    intent spelled as 'gitea' inside PACKS must classify noop against the same
    applied fingerprint — the alias folds into the identical enabled set, so
    nothing on the box changes except where the intent is written down.
    """
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,docuseal,moodle,filex",
            "ENABLE_GITEA": "true",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,docuseal,moodle,filex,gitea",
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
            completed_steps=tuple(
                phase
                for phase in applicable_phases_for(existing_desired)
                if phase != "cloudflare_access"
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "noop"


def test_modify_litellm_desired_only_local_alias_removal_reruns_consumers() -> None:
    raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder,moodle",
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
                "moodle",
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


def test_modify_matrix_hostname_change_reconciles_networking_and_matrix_only() -> None:
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_MATRIX": "true",
            "MATRIX_SUBDOMAIN": "chat",
            "CLOUDFLARE_ACCESS_OTP_EMAILS": "owner@example.com",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_MATRIX": "true",
            "MATRIX_SUBDOMAIN": "talk",
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
                "matrix",
                "cloudflare_access",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert "MATRIX_SUBDOMAIN" in plan.reasons[0]
    assert plan.start_phase == "networking"
    assert plan.phases_to_run == ("networking", "matrix")


def test_modify_schedules_shared_core_for_projection_drift_and_unfinished_tail() -> None:
    raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,moodle",
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
    assert "moodle" in plan.preserved_phases


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


def test_modify_pack_gaining_a_hostname_with_no_env_change_reruns_networking_and_pack() -> None:
    """A wizard upgrade that adds a pack hostname must converge, not reject.

    The live box hit this: the ONLYOFFICE sidecar joined the filex pack
    under the retired nextcloud pack's 'onlyoffice' key, so the desired
    state gained a hostname with no env change at all. A static key→phase
    map could not see the new key (the box rejected the modify outright);
    the mapping now derives from the catalog.
    """
    from dataclasses import replace

    raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,filex",
        }
    )
    legacy_desired = resolve_desired_state(raw)
    # The box converged BEFORE the sidecar joined the pack: its stored
    # snapshot carries no onlyoffice key. The upgraded catalog now emits
    # one with zero env change — that delta is the whole modify.
    existing_desired = replace(
        legacy_desired,
        hostnames={k: v for k, v in legacy_desired.hostnames.items() if k != "onlyoffice"},
    )
    requested_desired = legacy_desired
    assert "onlyoffice" not in existing_desired.hostnames
    assert requested_desired.hostnames["onlyoffice"] == "office.example.com"

    plan = classify_modify_request(
        existing_raw=raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=legacy_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "seaweedfs",
                "filex",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert set(plan.phases_to_run) >= {"networking", "filex"}
    assert "seaweedfs" in plan.preserved_phases


def test_modify_filex_addition_and_s3_de_expose_schedules_three_phases() -> None:
    """Migrating an old box: s3 was de-exposed and filex is being added.

    The stored desired state still models the public s3 hostname; the new
    catalog does not. The plan must rebind networking (route + DNS prune) and
    rerun seaweedfs (internal route) plus the new filex phase, while leaving
    docuseal preserved.
    """
    from dataclasses import replace

    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,docuseal",
        }
    )
    requested_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,docuseal,filex",
        }
    )
    legacy_desired = resolve_desired_state(existing_raw)
    existing_desired = replace(
        legacy_desired,
        hostnames={**legacy_desired.hostnames, "s3": "s3.example.com"},
    )
    requested_desired = resolve_desired_state(requested_raw)
    assert "s3" not in requested_desired.hostnames

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=legacy_desired.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "seaweedfs",
                "docuseal",
            ),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert set(plan.phases_to_run) >= {"networking", "seaweedfs", "filex"}
    assert "docuseal" in plan.preserved_phases


def test_modify_litellm_model_policy_flag_flip_reruns_shared_core_and_coder() -> None:
    """A workspace-key policy flip must re-sweep minted keys and re-seed the template.

    shared_core carries the reconcile sweep that re-scopes existing minted
    keys admin-side (no workspace reboot needed), and the coder phase re-bakes
    the MINTPY lane so future boots mint under the fresh policy. Non-consumer
    packs keep their evidence.
    """
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,gitea",
            "ALLOW_LITELLM_MODELS_FOR_NEW_USERS": "false",
        }
    )
    requested_raw = _raw(
        {
            **existing_raw.values,
            "ALLOW_LITELLM_MODELS_FOR_NEW_USERS": "true",
            "ALLOWED_LITELLM_ADDITIONAL_USERS": "bob,jo-ho",
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
            completed_steps=tuple(applicable_phases_for(existing_desired)),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert set(plan.phases_to_run) >= {"shared_core", "coder"}
    assert "gitea" in plan.preserved_phases


def test_modify_litellm_model_policy_default_valued_key_still_lands_one_rerun() -> None:
    """Adding the key with its default value still lands one rerun, then noops.

    The classifier routes on effective raw changes: the first pass that adds
    ALLOW_LITELLM_MODELS_FOR_NEW_USERS=false reruns shared_core and coder
    (the sweep converges and the template re-seeds even though the parsed
    value equals the built-in default). The second pass is a true noop once
    the on-box raw snapshot carries the same line — the exact sequence the
    box proof measures.
    """
    existing_raw = _raw(
        {
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "seaweedfs,coder,gitea",
        }
    )
    requested_raw = _raw(
        {
            **existing_raw.values,
            "ALLOW_LITELLM_MODELS_FOR_NEW_USERS": "false",
        }
    )
    existing_desired = resolve_desired_state(existing_raw)
    requested_desired = resolve_desired_state(requested_raw)
    checkpoint = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=requested_desired.fingerprint(),
        completed_steps=tuple(applicable_phases_for(requested_desired)),
    )

    plan = classify_modify_request(
        existing_raw=existing_raw,
        existing_desired=existing_desired,
        existing_applied=AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=existing_desired.fingerprint(),
            completed_steps=tuple(applicable_phases_for(existing_desired)),
        ),
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert plan.mode == "modify"
    assert set(plan.phases_to_run) >= {"shared_core", "coder"}

    second = classify_modify_request(
        existing_raw=requested_raw,
        existing_desired=requested_desired,
        existing_applied=checkpoint,
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        requested_raw=requested_raw,
        requested_desired=requested_desired,
    )

    assert second.mode == "noop"
