# pyright: reportMissingImports=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    DesiredState,
    RawEnvInput,
    StateValidationError,
    parse_env_file,
    resolve_desired_state,
)


def test_desired_state_resolution_is_deterministic(tmp_path: Path) -> None:
    env_a = tmp_path / "a.env"
    env_b = tmp_path / "b.env"

    env_a.write_text(
        "\n".join(
            [
                "STACK_NAME=my-stack",
                "ROOT_DOMAIN=example.com",
                "ENABLE_NEXTCLOUD=true",
                "ENABLE_OPENCLAW=true",
                "OPENCLAW_CHANNELS=telegram,telegram",
                "ENABLE_MATRIX=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_b.write_text(
        "\n".join(
            [
                "ENABLE_MATRIX=false",
                "OPENCLAW_CHANNELS=telegram",
                "ENABLE_OPENCLAW=true",
                "ROOT_DOMAIN=example.com",
                "STACK_NAME=my-stack",
                "ENABLE_NEXTCLOUD=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    desired_a = resolve_desired_state(parse_env_file(env_a))
    desired_b = resolve_desired_state(parse_env_file(env_b))

    assert desired_a.to_dict() == desired_b.to_dict()
    assert desired_a.fingerprint() == desired_b.fingerprint()
    assert desired_a.selected_packs == ("nextcloud", "openclaw")
    assert desired_a.enabled_packs == ("headscale", "nextcloud", "openclaw")
    assert desired_a.hostnames["onlyoffice"] == "office.example.com"
    assert desired_a.dokploy_api_url is None
    assert desired_a.openclaw_channels == ("telegram",)
    assert desired_a.openclaw_replicas == 1
    assert desired_a.shared_core.network_name == "my-stack-shared"
    assert desired_a.shared_core.postgres is not None
    assert desired_a.shared_core.redis is not None
    assert [allocation.pack_name for allocation in desired_a.shared_core.allocations] == [
        "nextcloud",
        "openclaw",
    ]


def test_models_round_trip_through_json() -> None:
    raw_input = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "my-stack",
            "ROOT_DOMAIN": "example.com",
        },
    )
    desired_state = DesiredState(
        format_version=1,
        stack_name="my-stack",
        root_domain="example.com",
        dokploy_url="https://dokploy.example.com",
        dokploy_api_url="https://dokploy.example.com",
        enable_tailscale=False,
        tailscale_hostname=None,
        tailscale_enable_ssh=False,
        tailscale_tags=(),
        tailscale_subnet_routes=(),
        cloudflare_access_otp_emails=(),
        enabled_features=("dokploy", "headscale"),
        selected_packs=(),
        enabled_packs=(),
        hostnames={
            "dokploy": "dokploy.example.com",
            "headscale": "headscale.example.com",
        },
        openclaw_channels=(),
        openclaw_replicas=None,
        my_farm_advisor_channels=(),
        my_farm_advisor_replicas=None,
        shared_core=resolve_desired_state(raw_input).shared_core,
    )
    applied_state = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=desired_state.fingerprint(),
        completed_steps=("preflight", "networking", "shared_core"),
    )

    raw_round_trip = RawEnvInput.from_dict(json.loads(json.dumps(raw_input.to_dict())))
    desired_round_trip = DesiredState.from_dict(json.loads(json.dumps(desired_state.to_dict())))
    applied_round_trip = AppliedStateCheckpoint.from_dict(
        json.loads(json.dumps(applied_state.to_dict()))
    )

    assert raw_round_trip == raw_input
    assert desired_round_trip == desired_state
    assert applied_round_trip == applied_state


def test_dokploy_api_config_requires_paired_url_and_key() -> None:
    with pytest.raises(StateValidationError, match="DOKPLOY_API_URL and DOKPLOY_API_KEY"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "my-stack",
                    "ROOT_DOMAIN": "example.com",
                    "DOKPLOY_API_URL": "https://dokploy.example.com",
                },
            )
        )


def test_dokploy_api_url_is_resolved_into_desired_state() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "my-stack",
                "ROOT_DOMAIN": "example.com",
                "DOKPLOY_API_URL": "https://dokploy.example.com/",
                "DOKPLOY_API_KEY": "dokp-key-123",
            },
        )
    )

    assert desired_state.dokploy_api_url == "https://dokploy.example.com"
