# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.lifecycle import (
    PHASE_ORDER,
    applicable_phases_for,
    validate_checkpoint_contract,
    validate_completed_steps,
)
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
    StateValidationError,
    parse_env_file,
    resolve_desired_state,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_root_env():
    return parse_env_file(_repo_root() / ".install.env")


def _index(phase: str) -> int:
    return PHASE_ORDER.index(phase)


def test_phase_order_preserves_infra_prereqs_but_uses_required_mvp_pack_sequence() -> None:
    assert _index("preflight") < _index("dokploy_bootstrap") < _index("networking")
    assert _index("networking") < _index("shared_core") < _index("seaweedfs")
    assert _index("seaweedfs") < _index("headscale") < _index("tailscale")
    assert _index("tailscale") < _index("nextcloud") < _index("coder") < _index("openclaw")
    assert _index("openclaw") < _index("my-farm-advisor")
    assert _index("openclaw") < _index("cloudflare_access")
    assert _index("my-farm-advisor") < _index("cloudflare_access")


def test_root_mvp_env_applicable_phases_follow_required_order_and_keep_coder() -> None:
    raw_env = _load_root_env()
    desired_state = resolve_desired_state(raw_env)

    assert raw_env.values["PACKS"] == "nextcloud,openclaw,seaweedfs,coder"
    assert "coder" in desired_state.enabled_packs
    assert desired_state.enable_tailscale is False

    assert applicable_phases_for(desired_state) == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "seaweedfs",
        "nextcloud",
        "coder",
        "openclaw",
        "cloudflare_access",
    )


def test_validate_completed_steps_accepts_required_new_prefix_for_root_mvp_env() -> None:
    raw_env = _load_root_env()
    applicable_phases = applicable_phases_for(resolve_desired_state(raw_env))

    validate_completed_steps(
        (
            "preflight",
            "dokploy_bootstrap",
            "networking",
            "shared_core",
            "seaweedfs",
        ),
        applicable_phases,
    )


def test_validate_completed_steps_rejects_old_prefix_when_mvp_order_changes() -> None:
    raw_env = _load_root_env()
    applicable_phases = applicable_phases_for(resolve_desired_state(raw_env))

    with pytest.raises(
        StateValidationError,
        match="Applied checkpoint does not match the supported lifecycle phase order",
    ):
        validate_completed_steps(
            (
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "cloudflare_access",
                "shared_core",
            ),
            applicable_phases,
        )


def test_validate_checkpoint_contract_rejects_legacy_checkpoint_version() -> None:
    raw_env = _load_root_env()
    desired_state = resolve_desired_state(raw_env)

    with pytest.raises(
        StateValidationError,
        match="lifecycle checkpoint contract version 1",
    ):
        validate_checkpoint_contract(
            AppliedStateCheckpoint(
                format_version=desired_state.format_version,
                desired_state_fingerprint=desired_state.fingerprint(),
                completed_steps=("preflight", "dokploy_bootstrap", "networking"),
                lifecycle_checkpoint_contract_version=1,
            ),
            applicable_phases_for(desired_state),
        )


def test_new_checkpoints_default_to_current_lifecycle_contract_version() -> None:
    raw_env = _load_root_env()
    desired_state = resolve_desired_state(raw_env)
    checkpoint = AppliedStateCheckpoint(
        format_version=desired_state.format_version,
        desired_state_fingerprint=desired_state.fingerprint(),
        completed_steps=("preflight",),
    )

    assert checkpoint.lifecycle_checkpoint_contract_version == LIFECYCLE_CHECKPOINT_CONTRACT_VERSION
