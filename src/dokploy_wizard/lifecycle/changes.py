# ruff: noqa: E501
"""Lifecycle change classification for rerun, resume, and modify flows."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from dokploy_wizard.core.models import SharedCorePlan
from dokploy_wizard.packs.catalog import (
    get_mutable_pack_env_keys,
    get_mutable_pack_resource_keys,
)
from dokploy_wizard.state.models import (
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
    AppliedStateCheckpoint,
    DesiredState,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
)

PHASE_ORDER: tuple[str, ...] = (
    "preflight",
    "dokploy_bootstrap",
    "networking",
    "shared_core",
    "seaweedfs",
    "headscale",
    "tailscale",
    "matrix",
    "nextcloud",
    "moodle",
    "docuseal",
    "filex",
    "coder",
    "cloudflare_access",
)

# LiteLLM is always installed as shared-core infrastructure, not as a standalone
# lifecycle phase or optional pack.
_LITELLM_PHASE = "shared_core"

_SUPPORTED_AUTH_KEYS = {
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_ZONE_ID",
}
_SUPPORTED_DOKPLOY_ADMIN_KEYS = {
    "DOKPLOY_ADMIN_EMAIL",
    "DOKPLOY_ADMIN_PASSWORD",
}
_SUPPORTED_TAILSCALE_KEYS = {
    "ENABLE_TAILSCALE",
    "TAILSCALE_AUTH_KEY",
    "TAILSCALE_HOSTNAME",
    "TAILSCALE_ENABLE_SSH",
    "TAILSCALE_TAGS",
    "TAILSCALE_SUBNET_ROUTES",
}
_SUPPORTED_ACCESS_KEYS = {"CLOUDFLARE_ACCESS_OTP_EMAILS"}
_SUPPORTED_HOSTNAME_KEYS = {
    "ROOT_DOMAIN",
    "DOKPLOY_SUBDOMAIN",
    "HEADSCALE_SUBDOMAIN",
    "MATRIX_SUBDOMAIN",
    "NEXTCLOUD_SUBDOMAIN",
    "ONLYOFFICE_SUBDOMAIN",
    "MOODLE_SUBDOMAIN",
    "DOCUSEAL_SUBDOMAIN",
    "FILEX_SUBDOMAIN",
    "CODER_SUBDOMAIN",
    "CODER_WILDCARD_SUBDOMAIN",
    "LITELLM_ADMIN_SUBDOMAIN",
}
# The LiteLLM admin hostname is derived from this key on demand instead of being
# modeled as a desired hostname, so it has no entry in _HOSTNAME_PHASES.
_LITELLM_ADMIN_HOSTNAME_ENV_KEYS = frozenset({"LITELLM_ADMIN_SUBDOMAIN"})
_SUPPORTED_ENABLEMENT_KEYS = {
    "PACKS",
    "ENABLE_HEADSCALE",
    "ENABLE_MATRIX",
    "ENABLE_NEXTCLOUD",
    "ENABLE_MOODLE",
    "ENABLE_DOCUSEAL",
    "ENABLE_FILEX",
    "ENABLE_CODER",
}
_SUPPORTED_MUTABLE_PACK_ENV_KEYS = set(get_mutable_pack_env_keys())
_SUPPORTED_MUTABLE_PACK_RESOURCE_KEYS = set(get_mutable_pack_resource_keys())
_REMOTE_HELPER_RAW_ENV_KEYS = {
    "VPS_HOST",
    "VPS_ROOT_PASSWORD",
}
_IGNORED_INSTALL_RAW_ENV_KEYS = {
    "CLOUDFLARE_API_TOKEN",
    "DOKPLOY_API_URL",
    "DOKPLOY_API_KEY",
    "DOKPLOY_ADMIN_EMAIL",
    "DOKPLOY_ADMIN_PASSWORD",
    "DOKPLOY_BOOTSTRAP_MOCK_API_KEY",
    "DOKPLOY_MOCK_API_MODE",
} | _REMOTE_HELPER_RAW_ENV_KEYS
_IGNORED_MODIFY_RAW_ENV_KEYS = {
    "DOKPLOY_API_URL",
    "DOKPLOY_API_KEY",
    "DOKPLOY_BOOTSTRAP_MOCK_API_KEY",
    "DOKPLOY_MOCK_API_MODE",
    "DOCKER_USERNAME",
    "DOCKER_PAT",
} | _REMOTE_HELPER_RAW_ENV_KEYS
_SUPPORTED_MODIFY_KEYS = (
    _SUPPORTED_AUTH_KEYS
    | _SUPPORTED_DOKPLOY_ADMIN_KEYS
    | _SUPPORTED_TAILSCALE_KEYS
    | _SUPPORTED_ACCESS_KEYS
    | _SUPPORTED_HOSTNAME_KEYS
    | _SUPPORTED_ENABLEMENT_KEYS
    | _SUPPORTED_MUTABLE_PACK_ENV_KEYS
    | _SUPPORTED_MUTABLE_PACK_RESOURCE_KEYS
)
_HOSTNAME_PHASES = {
    "dokploy": ("networking",),
    "headscale": ("networking", "headscale"),
    "matrix": ("networking", "matrix"),
    "nextcloud": ("networking", "nextcloud"),
    "onlyoffice": ("networking", "nextcloud"),
    "moodle": ("networking", "moodle"),
    "docuseal": ("networking", "docuseal"),
    "filex": ("networking", "filex"),
    # Legacy public s3 hostname: stacks installed before the de-expose still
    # model it as a desired hostname, and removing it has to schedule the
    # rebind of the SeaweedFS route onto the internal host plus the DNS prune.
    "s3": ("networking", "seaweedfs"),
    "coder": ("networking", "coder"),
    "coder-wildcard": ("networking", "coder"),
}
_OUTBOUND_MAIL_ENV_KEYS = {
    "OUTBOUND_SMTP_HOSTNAME",
    "OUTBOUND_SMTP_FROM_ADDRESS",
}
_CODER_RUNTIME_ENV_KEYS = {
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_MODEL",
    # Which Hermes build the Desktop workspace installs is what decides whether the
    # browser-hosted Desktop exists at all, so a change has to re-push the template.
    "HERMES_DESKTOP_CHANNEL",
    "AI_DEFAULT_API_KEY",
    "AI_DEFAULT_BASE_URL",
    "AI_DEFAULT_PROVIDER",
    "AI_DEFAULT_MODEL",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_GO_BASE_URL",
    "LITELLM_NVIDIA_API_KEY",
    "LITELLM_NVIDIA_BASE_URL",
    "LITELLM_NVIDIA_MODELS",
    "NVIDIA_BASE_URL",
}
_LITELLM_SHARED_CONFIG_ENV_KEYS = {
    "AI_DEFAULT_API_KEY",
    "AI_DEFAULT_BASE_URL",
    "AI_DEFAULT_PROVIDER",
    "AI_DEFAULT_MODEL",
    # The pattern is rendered into the model-sync sources document, so a change has to
    # re-push shared core config the same way a key change does; the Coder templates
    # bake the literal AI_DEFAULT_MODEL and are unaffected either way.
    "AI_DEFAULT_MODEL_PATTERN",
    "LITELLM_OPENCODE_GO_API_KEY",
    "LITELLM_OPENROUTER_API_KEY",
    "LITELLM_NVIDIA_API_KEY",
    "LITELLM_NVIDIA_BASE_URL",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_GO_BASE_URL",
    "NVIDIA_BASE_URL",
    "LITELLM_LOCAL_BASE_URL",
    "LITELLM_LOCAL_MODEL",
    "LITELLM_LOCAL_API_KEY",
    "LITELLM_OPENROUTER_MODELS",
    "LITELLM_NVIDIA_MODELS",
    "LITELLM_OPENCODE_GO_WILDCARD",
    "OPENROUTER_API_KEY",
}
_LITELLM_CONSUMER_PHASES = {"coder"}

_SUPPORTED_MODIFY_KEYS |= (
    _OUTBOUND_MAIL_ENV_KEYS
    | _CODER_RUNTIME_ENV_KEYS
    | _LITELLM_SHARED_CONFIG_ENV_KEYS
)

@dataclass(frozen=True)
class LifecyclePlan:
    mode: str
    reasons: tuple[str, ...]
    applicable_phases: tuple[str, ...]
    phases_to_run: tuple[str, ...]
    preserved_phases: tuple[str, ...]
    initial_completed_steps: tuple[str, ...]
    start_phase: str | None
    raw_equivalent: bool
    desired_equivalent: bool


def applicable_phases_for(desired_state: DesiredState) -> tuple[str, ...]:
    phases = {
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
    }
    if "seaweedfs" in desired_state.enabled_packs:
        phases.add("seaweedfs")
    if "headscale" in desired_state.selected_packs:
        phases.add("headscale")
    if desired_state.enable_tailscale:
        phases.add("tailscale")
    if "matrix" in desired_state.enabled_packs:
        phases.add("matrix")
    if "nextcloud" in desired_state.enabled_packs:
        phases.add("nextcloud")
    if "moodle" in desired_state.enabled_packs:
        phases.add("moodle")
    if "docuseal" in desired_state.enabled_packs:
        phases.add("docuseal")
    if "filex" in desired_state.enabled_packs:
        phases.add("filex")
    if "coder" in desired_state.enabled_packs:
        phases.add("coder")
    if _access_enabled(desired_state):
        phases.add("cloudflare_access")
    return tuple(phase for phase in PHASE_ORDER if phase in phases)


def validate_completed_steps(
    completed_steps: tuple[str, ...], applicable_phases: tuple[str, ...]
) -> None:
    expected_prefix = applicable_phases[: len(completed_steps)]
    if completed_steps != expected_prefix:
        msg = (
            "Applied checkpoint does not match the supported lifecycle phase order. "
            f"Expected prefix {list(expected_prefix)}, found {list(completed_steps)}."
        )
        raise StateValidationError(msg)


def validate_checkpoint_contract(
    applied_state: AppliedStateCheckpoint, applicable_phases: tuple[str, ...]
) -> None:
    if applied_state.lifecycle_checkpoint_contract_version != LIFECYCLE_CHECKPOINT_CONTRACT_VERSION:
        msg = (
            "Applied checkpoint uses lifecycle checkpoint contract version "
            f"{applied_state.lifecycle_checkpoint_contract_version}, but the current "
            f"contract version is {LIFECYCLE_CHECKPOINT_CONTRACT_VERSION}. "
            "Only empty install scaffolds can be restarted across contract versions; "
            "existing non-empty checkpoints must not be resumed or modified under the new order."
        )
        raise StateValidationError(msg)
    validate_completed_steps(applied_state.completed_steps, applicable_phases)


def classify_install_request(
    *,
    existing_raw: RawEnvInput,
    existing_desired: DesiredState,
    existing_applied: AppliedStateCheckpoint,
    requested_raw: RawEnvInput,
    requested_desired: DesiredState,
) -> LifecyclePlan:
    raw_equivalent = _normalized_install_raw_values(existing_raw) == _normalized_install_raw_values(
        requested_raw
    )
    desired_equivalent = _normalized_install_desired_values(
        existing_desired
    ) == _normalized_install_desired_values(requested_desired)
    if not raw_equivalent or not desired_equivalent:
        raise StateValidationError(
            "Existing install state does not match this install request. "
            "Use 'modify' for supported lifecycle changes."
        )
    return _classify_same_target(
        existing_applied=existing_applied,
        desired_state=requested_desired,
        raw_equivalent=raw_equivalent,
        desired_equivalent=desired_equivalent,
    )


def classify_modify_request(
    *,
    existing_raw: RawEnvInput,
    existing_desired: DesiredState,
    existing_applied: AppliedStateCheckpoint,
    existing_ledger: OwnershipLedger,
    requested_raw: RawEnvInput,
    requested_desired: DesiredState,
) -> LifecyclePlan:
    old_applicable = applicable_phases_for(existing_desired)
    validate_checkpoint_contract(existing_applied, old_applicable)
    raw_equivalent = _normalized_modify_raw_values(existing_raw) == _normalized_modify_raw_values(
        requested_raw
    )
    desired_equivalent = _normalized_modify_desired_values(
        existing_desired
    ) == _normalized_modify_desired_values(requested_desired)
    if raw_equivalent and desired_equivalent:
        return _classify_same_target(
            existing_applied=existing_applied,
            desired_state=requested_desired,
            raw_equivalent=True,
            desired_equivalent=True,
        )

    reasons: list[str] = []
    changed_keys = _changed_env_keys(existing_raw, requested_raw)
    effective_changed_keys = set(changed_keys)
    if desired_equivalent:
        effective_changed_keys -= _SUPPORTED_ENABLEMENT_KEYS
    removed_packs = set(existing_desired.enabled_packs) - set(requested_desired.enabled_packs)
    unsupported_keys = sorted(effective_changed_keys - _SUPPORTED_MODIFY_KEYS - {"STACK_NAME"})
    if existing_desired.stack_name != requested_desired.stack_name:
        reasons.append("STACK_NAME changes are unsupported in Task 11.")
    if unsupported_keys:
        reasons.append(f"Unsupported mutable env keys for Task 11: {unsupported_keys}.")

    if reasons:
        raise StateValidationError(" ".join(reasons))

    if not effective_changed_keys and desired_equivalent:
        return _classify_same_target(
            existing_applied=existing_applied,
            desired_state=requested_desired,
            raw_equivalent=raw_equivalent,
            desired_equivalent=True,
        )

    phases_to_run: set[str] = set()
    litellm_desired_projection_changed = _desired_diff_is_only_litellm_projection(
        existing_desired, requested_desired
    )
    tailscale_disable_only = (
        existing_desired.enable_tailscale and not requested_desired.enable_tailscale
    )
    if effective_changed_keys & _SUPPORTED_TAILSCALE_KEYS and requested_desired.enable_tailscale:
        phases_to_run.add("tailscale")
    if effective_changed_keys & {"DOKPLOY_ADMIN_EMAIL", "DOKPLOY_ADMIN_PASSWORD"}:
        if "nextcloud" in requested_desired.enabled_packs:
            phases_to_run.add("nextcloud")
    if effective_changed_keys & _SUPPORTED_AUTH_KEYS:
        phases_to_run.add("networking")
    if effective_changed_keys & (_SUPPORTED_AUTH_KEYS | _SUPPORTED_ACCESS_KEYS) and _access_enabled(
        requested_desired
    ):
        phases_to_run.add("cloudflare_access")
    if (
        effective_changed_keys & _CODER_RUNTIME_ENV_KEYS
        and "coder" in requested_desired.enabled_packs
    ):
        phases_to_run.add("coder")
    if (effective_changed_keys & _LITELLM_SHARED_CONFIG_ENV_KEYS) or litellm_desired_projection_changed:
        phases_to_run.add(_LITELLM_PHASE)
        phases_to_run.update(
            _litellm_dependent_consumer_phases(effective_changed_keys, requested_desired)
        )
        if litellm_desired_projection_changed:
            phases_to_run.update(
                _litellm_dependent_consumer_phases(
                    _LITELLM_SHARED_CONFIG_ENV_KEYS,
                    requested_desired,
                )
            )
    if (
        existing_desired.cloudflare_access_otp_emails
        != requested_desired.cloudflare_access_otp_emails
        and _access_enabled(requested_desired)
    ):
        phases_to_run.add("cloudflare_access")
    if effective_changed_keys & _OUTBOUND_MAIL_ENV_KEYS:
        phases_to_run.add("shared_core")
        if "moodle" in requested_desired.enabled_packs:
            phases_to_run.add("moodle")
        if "docuseal" in requested_desired.enabled_packs:
            phases_to_run.add("docuseal")
    if (
        effective_changed_keys
        and existing_desired.shared_core.to_dict() != requested_desired.shared_core.to_dict()
    ):
        phases_to_run.add("shared_core")
    elif _model_sync_projection_drift_only(
        existing_desired.shared_core, requested_desired.shared_core
    ):
        # A wizard upgrade added or removed the model-sync sidecar projection
        # with identical env input; the idempotent shared_core phase applies
        # or prunes the sidecar service inside the existing compose app.
        phases_to_run.add("shared_core")
    if set(removed_packs) & _LITELLM_CONSUMER_PHASES:
        phases_to_run.add("shared_core")
    phases_to_run.update(_hostname_change_phases(existing_desired, requested_desired))
    if "ROOT_DOMAIN" in effective_changed_keys and _access_enabled(requested_desired):
        # A domain rotation rewrites every hostname, including the derived LiteLLM
        # admin hostname the Access application protects, so the policy has to be
        # re-applied or the admin UI comes up unguarded on the new domain.
        phases_to_run.add("cloudflare_access")
    if effective_changed_keys & _LITELLM_ADMIN_HOSTNAME_ENV_KEYS:
        # networking publishes the DNS record and the Access application for the admin
        # hostname, and shared_core re-renders the LiteLLM service that advertises it.
        phases_to_run.update({"networking", _LITELLM_PHASE})
        if _access_enabled(requested_desired):
            phases_to_run.add("cloudflare_access")
    phases_to_run.update(_new_pack_phases(existing_desired, requested_desired))

    access_disable_only = bool(existing_desired.cloudflare_access_otp_emails) and not bool(
        requested_desired.cloudflare_access_otp_emails
    )
    if not phases_to_run and not tailscale_disable_only and not access_disable_only:
        raise StateValidationError(
            "Requested modify operation changes values that are not modeled as supported "
            "runtime mutations in Task 11."
        )

    applicable_phases = applicable_phases_for(requested_desired)
    completed_intersection = set(existing_applied.completed_steps) & set(applicable_phases)
    if set(applicable_phases) - completed_intersection - phases_to_run:
        # A previous lifecycle run stopped before finishing (for example a
        # failed install phase); modify must finish the unfinished tail even
        # when the requested change itself only touches other phases.
        phases_to_run.update(set(applicable_phases) - completed_intersection)
    preserved_phases = tuple(
        phase
        for phase in applicable_phases
        if phase in completed_intersection and phase not in phases_to_run
    )
    initial_completed_steps = _longest_valid_prefix(applicable_phases, set(preserved_phases))
    start_phase = next((phase for phase in applicable_phases if phase in phases_to_run), None)
    return LifecyclePlan(
        mode="modify",
        reasons=_sorted_reasons(changed_keys, phases_to_run),
        applicable_phases=applicable_phases,
        phases_to_run=tuple(phase for phase in applicable_phases if phase in phases_to_run),
        preserved_phases=preserved_phases,
        initial_completed_steps=initial_completed_steps,
        start_phase=start_phase,
        raw_equivalent=raw_equivalent,
        desired_equivalent=desired_equivalent,
    )


def force_shared_core_refresh(
    *,
    existing_applied: AppliedStateCheckpoint,
    desired_state: DesiredState,
    reason: str,
) -> LifecyclePlan:
    """Plan used by ``upgrade --litellm`` when nothing else changed.

    The image pin lives in state, so a tag bump leaves the raw env and the
    desired state byte-identical; the same-target classifier would answer
    ``noop`` and the freshly pinned image would never be rendered into the
    compose app. This plan forces one real ``shared_core`` pass, which
    re-renders compose from the pin and deploys it while every other phase
    keeps its preserved evidence.
    """

    applicable_phases = applicable_phases_for(desired_state)
    validate_checkpoint_contract(existing_applied, applicable_phases)
    return LifecyclePlan(
        mode="modify",
        reasons=(reason,),
        applicable_phases=applicable_phases,
        phases_to_run=(_LITELLM_PHASE,),
        preserved_phases=tuple(
            phase for phase in applicable_phases if phase != _LITELLM_PHASE
        ),
        initial_completed_steps=applicable_phases,
        start_phase=_LITELLM_PHASE,
        raw_equivalent=True,
        desired_equivalent=True,
    )


def force_phase_into_plan(plan: LifecyclePlan, *, phase: str, reason: str) -> LifecyclePlan:
    """Make one already-completed phase run again inside an existing plan.

    ``upgrade --litellm`` repins the shared-core image out of band, so the
    classification it triggers may be ``noop`` (nothing in the env changed) or
    a ``modify`` that never touches ``shared_core`` (some unrelated key moved).
    Both answers would leave the freshly pinned image unreached; this helper
    folds the phase into either plan shape, preserving every other phase's
    evidence.
    """

    if phase in plan.phases_to_run:
        return plan
    if phase not in plan.applicable_phases:
        return plan
    if plan.mode == "noop":
        preserved_phases = tuple(p for p in plan.applicable_phases if p != phase)
        return replace(
            plan,
            mode="modify",
            reasons=(reason,),
            phases_to_run=(phase,),
            preserved_phases=preserved_phases,
            initial_completed_steps=_longest_valid_prefix(
                plan.applicable_phases, set(preserved_phases) | {phase}
            ),
            start_phase=phase,
        )
    preserved_phases = tuple(p for p in plan.preserved_phases if p != phase)
    phases_to_run = tuple(
        sorted(
            set(plan.phases_to_run) | {phase},
            key=lambda item: plan.applicable_phases.index(item),
        )
    )
    return replace(
        plan,
        reasons=(*plan.reasons, reason),
        phases_to_run=phases_to_run,
        preserved_phases=preserved_phases,
        initial_completed_steps=_longest_valid_prefix(
            plan.applicable_phases, set(preserved_phases) | {phase}
        ),
        start_phase=next(
            (item for item in plan.applicable_phases if item in phases_to_run),
            plan.start_phase,
        ),
    )


def _classify_same_target(
    *,
    existing_applied: AppliedStateCheckpoint,
    desired_state: DesiredState,
    raw_equivalent: bool,
    desired_equivalent: bool,
) -> LifecyclePlan:
    applicable_phases = applicable_phases_for(desired_state)
    validate_checkpoint_contract(existing_applied, applicable_phases)
    if (
        existing_applied.completed_steps != applicable_phases
        and existing_applied.desired_state_fingerprint != desired_state.fingerprint()
    ):
        return LifecyclePlan(
            mode="resume",
            reasons=(
                "Existing checkpoint is incomplete and targets an older desired state; "
                "rerunning from the start conservatively.",
            ),
            applicable_phases=applicable_phases,
            phases_to_run=applicable_phases,
            preserved_phases=(),
            initial_completed_steps=(),
            start_phase=applicable_phases[0] if applicable_phases else None,
            raw_equivalent=raw_equivalent,
            desired_equivalent=desired_equivalent,
        )
    if existing_applied.completed_steps == applicable_phases:
        return LifecyclePlan(
            mode="noop",
            reasons=("Requested raw input and desired state match the persisted target.",),
            applicable_phases=applicable_phases,
            phases_to_run=(),
            preserved_phases=applicable_phases,
            initial_completed_steps=applicable_phases,
            start_phase=None,
            raw_equivalent=raw_equivalent,
            desired_equivalent=desired_equivalent,
        )
    preserved_phases = existing_applied.completed_steps
    remaining = applicable_phases[len(existing_applied.completed_steps) :]
    return LifecyclePlan(
        mode="resume",
        reasons=(
            "Existing checkpoint is incomplete; resuming from the last persisted successful "
            "phase prefix.",
        ),
        applicable_phases=applicable_phases,
        phases_to_run=remaining,
        preserved_phases=preserved_phases,
        initial_completed_steps=preserved_phases,
        start_phase=remaining[0],
        raw_equivalent=raw_equivalent,
        desired_equivalent=desired_equivalent,
    )


def _hostname_change_phases(
    existing_desired: DesiredState, requested_desired: DesiredState
) -> set[str]:
    changed: set[str] = set()
    all_keys = set(existing_desired.hostnames) | set(requested_desired.hostnames)
    for key in sorted(all_keys):
        if existing_desired.hostnames.get(key) == requested_desired.hostnames.get(key):
            continue
        changed.update(_HOSTNAME_PHASES.get(key, ()))
    return changed


def _new_pack_phases(existing_desired: DesiredState, requested_desired: DesiredState) -> set[str]:
    phases: set[str] = set()
    new_packs = set(requested_desired.enabled_packs) - set(existing_desired.enabled_packs)
    if not new_packs:
        return phases
    if any(
        pack in new_packs
        for pack in {"matrix", "nextcloud", "coder"}
    ):
        phases.add("shared_core")
    if "headscale" in new_packs:
        phases.add("headscale")
    if "matrix" in new_packs:
        phases.add("matrix")
    if "nextcloud" in new_packs:
        phases.add("nextcloud")
    if "seaweedfs" in new_packs:
        phases.add("seaweedfs")
    if "filex" in new_packs:
        phases.add("filex")
    if "coder" in new_packs:
        phases.add("coder")
    if (
        "headscale" in requested_desired.selected_packs
        and "headscale" not in existing_desired.selected_packs
    ):
        phases.add("headscale")
    return phases


def _changed_env_keys(existing_raw: RawEnvInput, requested_raw: RawEnvInput) -> set[str]:
    all_keys = (set(existing_raw.values) | set(requested_raw.values)) - _IGNORED_MODIFY_RAW_ENV_KEYS
    return {
        key for key in all_keys if existing_raw.values.get(key) != requested_raw.values.get(key)
    }


def _normalized_install_raw_values(raw_env: RawEnvInput) -> dict[str, str]:
    return {
        key: value
        for key, value in raw_env.values.items()
        if key not in _IGNORED_INSTALL_RAW_ENV_KEYS
    }


def _normalized_modify_raw_values(raw_env: RawEnvInput) -> dict[str, str]:
    return {
        key: value
        for key, value in raw_env.values.items()
        if key not in _IGNORED_MODIFY_RAW_ENV_KEYS
    }


def _normalized_install_desired_values(desired_state: DesiredState) -> dict[str, object]:
    payload = desired_state.to_dict()
    payload["dokploy_api_url"] = None
    return payload


def _normalized_modify_desired_values(desired_state: DesiredState) -> dict[str, object]:
    payload = desired_state.to_dict()
    payload["dokploy_api_url"] = None
    return payload


def _longest_valid_prefix(
    applicable_phases: tuple[str, ...], valid_phases: set[str]
) -> tuple[str, ...]:
    prefix: list[str] = []
    for phase in applicable_phases:
        if phase not in valid_phases:
            break
        prefix.append(phase)
    return tuple(prefix)


def _sorted_reasons(changed_keys: set[str], phases_to_run: set[str]) -> tuple[str, ...]:
    scheduled_phases = [phase for phase in PHASE_ORDER if phase in phases_to_run]
    return (
        f"Supported modify keys changed: {sorted(changed_keys)}.",
        f"Lifecycle phases scheduled in fixed order: {scheduled_phases}.",
    )


def _access_enabled(desired_state: DesiredState) -> bool:
    # Gate on the operator's intent: the hostnames Access protects come from
    # _access_target_hostnames, and no surviving pack implies Access on its own.
    return bool(desired_state.cloudflare_access_otp_emails)


def _model_sync_projection_drift_only(
    existing: SharedCorePlan, requested: SharedCorePlan
) -> bool:
    if existing.model_sync == requested.model_sync:
        return False
    return replace(existing, model_sync=None) == replace(requested, model_sync=None)


def _desired_diff_is_only_litellm_projection(
    existing_desired: DesiredState, requested_desired: DesiredState
) -> bool:
    if _litellm_desired_projection(existing_desired) == _litellm_desired_projection(
        requested_desired
    ):
        return False
    existing_payload = existing_desired.to_dict()
    requested_payload = requested_desired.to_dict()
    _drop_litellm_desired_projection(existing_payload)
    _drop_litellm_desired_projection(requested_payload)
    return existing_payload == requested_payload


def _litellm_desired_projection(desired_state: DesiredState) -> tuple[str, ...] | None:
    litellm = desired_state.shared_core.litellm
    if litellm is None:
        return None
    return litellm.default_model_alias_order


def _drop_litellm_desired_projection(payload: dict[str, Any]) -> None:
    shared_core = payload.get("shared_core")
    if not isinstance(shared_core, dict):
        return
    litellm = shared_core.get("litellm")
    if isinstance(litellm, dict):
        litellm.pop("default_model_alias_order", None)


def _litellm_dependent_consumer_phases(
    changed_keys: set[str], desired_state: DesiredState
) -> set[str]:
    phases: set[str] = set()
    enabled_packs = set(desired_state.enabled_packs)
    if changed_keys & _LITELLM_SHARED_CONFIG_ENV_KEYS:
        phases.update(enabled_packs & _LITELLM_CONSUMER_PHASES)
    return phases
