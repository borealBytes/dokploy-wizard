"""Lifecycle change classification for rerun, resume, and modify flows."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.packs.catalog import (
    get_mutable_pack_env_keys,
    get_mutable_pack_resource_keys,
)
from dokploy_wizard.state.models import (
    AppliedStateCheckpoint,
    DesiredState,
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
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
    "coder",
    "openclaw",
    "my-farm-advisor",
    "cloudflare_access",
)

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
    "CODER_SUBDOMAIN",
    "CODER_WILDCARD_SUBDOMAIN",
    "OPENCLAW_SUBDOMAIN",
    "OPENCLAW_INTERNAL_SUBDOMAIN",
    "MY_FARM_ADVISOR_SUBDOMAIN",
}
_SUPPORTED_ENABLEMENT_KEYS = {
    "PACKS",
    "ENABLE_HEADSCALE",
    "ENABLE_MATRIX",
    "ENABLE_NEXTCLOUD",
    "ENABLE_MOODLE",
    "ENABLE_DOCUSEAL",
    "ENABLE_CODER",
    "ENABLE_OPENCLAW",
    "ENABLE_MY_FARM_ADVISOR",
}
_SUPPORTED_MUTABLE_PACK_ENV_KEYS = set(get_mutable_pack_env_keys())
_SUPPORTED_MUTABLE_PACK_RESOURCE_KEYS = set(get_mutable_pack_resource_keys())
_IGNORED_INSTALL_RAW_ENV_KEYS = {
    "CLOUDFLARE_API_TOKEN",
    "DOKPLOY_API_URL",
    "DOKPLOY_API_KEY",
    "DOKPLOY_ADMIN_EMAIL",
    "DOKPLOY_ADMIN_PASSWORD",
    "DOKPLOY_BOOTSTRAP_MOCK_API_KEY",
    "DOKPLOY_MOCK_API_MODE",
}
_IGNORED_MODIFY_RAW_ENV_KEYS = {
    "DOKPLOY_API_URL",
    "DOKPLOY_API_KEY",
    "DOKPLOY_BOOTSTRAP_MOCK_API_KEY",
    "DOKPLOY_MOCK_API_MODE",
}
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
    "s3": ("networking", "seaweedfs"),
    "coder": ("networking", "coder"),
    "coder-wildcard": ("networking", "coder"),
    "openclaw": ("networking", "openclaw"),
    "openclaw-internal": ("openclaw",),
    "my-farm-advisor": ("networking", "my-farm-advisor"),
}
_OPTIONAL_PHASE_PACKS = {
    "matrix": "matrix",
    "nextcloud": "nextcloud",
    "moodle": "moodle",
    "docuseal": "docuseal",
    "seaweedfs": "seaweedfs",
    "coder": "coder",
    "openclaw": "openclaw",
    "my-farm-advisor": "my-farm-advisor",
}
_OPENCLAW_RUNTIME_ENV_KEYS = {
    "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL",
    "OPENCLAW_NEXA_TALK_SHARED_SECRET",
    "OPENCLAW_NEXA_TALK_SIGNING_SECRET",
    "OPENCLAW_NEXA_ONLYOFFICE_CALLBACK_SECRET",
    "OPENCLAW_NEXA_EDITOR_EVENTS_SHARED_SECRET",
    "OPENCLAW_NEXA_WEBDAV_AUTH_USER",
    "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD",
    "OPENCLAW_NEXA_AGENT_USER_ID",
    "OPENCLAW_NEXA_AGENT_DISPLAY_NAME",
    "OPENCLAW_NEXA_AGENT_PASSWORD",
    "OPENCLAW_NEXA_AGENT_EMAIL",
    "OPENCLAW_NEXA_MEM0_BASE_URL",
    "OPENCLAW_NEXA_MEM0_API_KEY",
    "OPENCLAW_NEXA_MEM0_LLM_BASE_URL",
    "OPENCLAW_NEXA_MEM0_LLM_API_KEY",
    "OPENCLAW_NEXA_MEM0_EMBEDDER_MODEL",
    "OPENCLAW_NEXA_MEM0_EMBEDDER_DIMENSIONS",
    "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND",
    "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL",
    "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY",
    "OPENCLAW_NEXA_MEM0_VECTOR_DIMENSIONS",
    "OPENCLAW_NEXA_PRESENCE_POLICY",
    "OPENCLAW_OPENROUTER_API_KEY",
    "OPENCLAW_NVIDIA_API_KEY",
    "OPENCLAW_PRIMARY_MODEL",
    "OPENCLAW_FALLBACK_MODELS",
    "OPENCLAW_TELEGRAM_BOT_TOKEN",
    "OPENCLAW_TELEGRAM_OWNER_USER_ID",
}
_NEXTCLOUD_NEXA_USER_ENV_KEYS = {
    "OPENCLAW_NEXA_AGENT_USER_ID",
    "OPENCLAW_NEXA_AGENT_DISPLAY_NAME",
    "OPENCLAW_NEXA_AGENT_PASSWORD",
    "OPENCLAW_NEXA_AGENT_EMAIL",
}
_MY_FARM_RUNTIME_ENV_KEYS = {
    "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
    "MY_FARM_ADVISOR_NVIDIA_API_KEY",
    "MY_FARM_ADVISOR_PRIMARY_MODEL",
    "MY_FARM_ADVISOR_FALLBACK_MODELS",
    "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
    "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID",
}
_OUTBOUND_MAIL_ENV_KEYS = {
    "OUTBOUND_SMTP_HOSTNAME",
    "OUTBOUND_SMTP_FROM_ADDRESS",
}
_CODER_RUNTIME_ENV_KEYS = {
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_MODEL",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_GO_BASE_URL",
}

_SUPPORTED_MODIFY_KEYS |= (
    _OPENCLAW_RUNTIME_ENV_KEYS
    | _NEXTCLOUD_NEXA_USER_ENV_KEYS
    | _MY_FARM_RUNTIME_ENV_KEYS
    | _OUTBOUND_MAIL_ENV_KEYS
    | _CODER_RUNTIME_ENV_KEYS
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
    if "coder" in desired_state.enabled_packs:
        phases.add("coder")
    if "openclaw" in desired_state.enabled_packs:
        phases.add("openclaw")
    if "my-farm-advisor" in desired_state.enabled_packs:
        phases.add("my-farm-advisor")
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
    desired_equivalent = existing_desired.to_dict() == requested_desired.to_dict()
    if raw_equivalent and desired_equivalent:
        return _classify_same_target(
            existing_applied=existing_applied,
            desired_state=requested_desired,
            raw_equivalent=True,
            desired_equivalent=True,
        )

    reasons: list[str] = []
    changed_keys = _changed_env_keys(existing_raw, requested_raw)
    removed_packs = sorted(
        set(existing_desired.enabled_packs) - set(requested_desired.enabled_packs)
    )
    unsupported_keys = sorted(changed_keys - _SUPPORTED_MODIFY_KEYS - {"STACK_NAME"})
    if existing_desired.stack_name != requested_desired.stack_name:
        reasons.append("STACK_NAME changes are unsupported in Task 11.")
    if unsupported_keys:
        reasons.append(f"Unsupported mutable env keys for Task 11: {unsupported_keys}.")

    if reasons:
        raise StateValidationError(" ".join(reasons))

    phases_to_run: set[str] = set()
    tailscale_disable_only = (
        existing_desired.enable_tailscale and not requested_desired.enable_tailscale
    )
    if changed_keys & _SUPPORTED_TAILSCALE_KEYS and requested_desired.enable_tailscale:
        phases_to_run.add("tailscale")
    if changed_keys & {"DOKPLOY_ADMIN_EMAIL", "DOKPLOY_ADMIN_PASSWORD"}:
        if "nextcloud" in requested_desired.enabled_packs:
            phases_to_run.add("nextcloud")
    if changed_keys & _SUPPORTED_AUTH_KEYS:
        phases_to_run.add("networking")
    if changed_keys & (_SUPPORTED_AUTH_KEYS | _SUPPORTED_ACCESS_KEYS) and _access_enabled(
        requested_desired
    ):
        phases_to_run.add("cloudflare_access")
    if changed_keys & _CODER_RUNTIME_ENV_KEYS and "coder" in requested_desired.enabled_packs:
        phases_to_run.add("coder")
    if (
        existing_desired.cloudflare_access_otp_emails
        != requested_desired.cloudflare_access_otp_emails
        and _access_enabled(requested_desired)
    ):
        phases_to_run.add("cloudflare_access")
        if "openclaw" in requested_desired.enabled_packs:
            phases_to_run.add("openclaw")
        if "my-farm-advisor" in requested_desired.enabled_packs:
            phases_to_run.add("my-farm-advisor")
    if existing_desired.openclaw_channels != requested_desired.openclaw_channels:
        phases_to_run.add("openclaw")
    if existing_desired.openclaw_gateway_token != requested_desired.openclaw_gateway_token:
        phases_to_run.add("openclaw")
    if existing_desired.openclaw_replicas != requested_desired.openclaw_replicas:
        phases_to_run.add("openclaw")
    if changed_keys & _OPENCLAW_RUNTIME_ENV_KEYS:
        phases_to_run.add("openclaw")
    if changed_keys & _NEXTCLOUD_NEXA_USER_ENV_KEYS:
        phases_to_run.add("nextcloud")
    if existing_desired.my_farm_advisor_channels != requested_desired.my_farm_advisor_channels:
        phases_to_run.add("my-farm-advisor")
    if existing_desired.my_farm_advisor_replicas != requested_desired.my_farm_advisor_replicas:
        phases_to_run.add("my-farm-advisor")
    if changed_keys & _MY_FARM_RUNTIME_ENV_KEYS:
        phases_to_run.add("my-farm-advisor")
    if changed_keys & _OUTBOUND_MAIL_ENV_KEYS:
        phases_to_run.add("shared_core")
        if "moodle" in requested_desired.enabled_packs:
            phases_to_run.add("moodle")
        if "docuseal" in requested_desired.enabled_packs:
            phases_to_run.add("docuseal")
    if (
        existing_desired.shared_core.to_dict() != requested_desired.shared_core.to_dict()
        and not removed_packs
    ):
        phases_to_run.add("shared_core")
    phases_to_run.update(_hostname_change_phases(existing_desired, requested_desired))
    phases_to_run.update(_new_pack_phases(existing_desired, requested_desired))

    if not phases_to_run and not tailscale_disable_only:
        raise StateValidationError(
            "Requested modify operation changes values that are not modeled as supported "
            "runtime mutations in Task 11."
        )

    applicable_phases = applicable_phases_for(requested_desired)
    completed_intersection = set(existing_applied.completed_steps) & set(applicable_phases)
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


def _classify_same_target(
    *,
    existing_applied: AppliedStateCheckpoint,
    desired_state: DesiredState,
    raw_equivalent: bool,
    desired_equivalent: bool,
) -> LifecyclePlan:
    applicable_phases = applicable_phases_for(desired_state)
    validate_checkpoint_contract(existing_applied, applicable_phases)
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
        if key in {"openclaw", "my-farm-advisor"} and _access_enabled(requested_desired):
            changed.add("cloudflare_access")
    return changed


def _new_pack_phases(existing_desired: DesiredState, requested_desired: DesiredState) -> set[str]:
    phases: set[str] = set()
    new_packs = set(requested_desired.enabled_packs) - set(existing_desired.enabled_packs)
    if not new_packs:
        return phases
    if any(
        pack in new_packs
        for pack in {"matrix", "nextcloud", "coder", "openclaw", "my-farm-advisor"}
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
    if "coder" in new_packs:
        phases.add("coder")
    if "openclaw" in new_packs:
        if _access_enabled(requested_desired):
            phases.add("cloudflare_access")
        phases.add("openclaw")
    if "my-farm-advisor" in new_packs:
        if _access_enabled(requested_desired):
            phases.add("cloudflare_access")
        phases.add("my-farm-advisor")
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
    return bool({"openclaw", "my-farm-advisor"} & set(desired_state.enabled_packs))
