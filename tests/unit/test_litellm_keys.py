# pyright: reportMissingImports=false

from __future__ import annotations

import json
from pathlib import Path

from dokploy_wizard import cli
from dokploy_wizard.state import (
    LITELLM_GENERATED_KEYS_FILE,
    LiteLLMGeneratedKeys,
    RawEnvInput,
    ensure_litellm_generated_keys,
    resolve_desired_state,
    write_litellm_generated_keys,
)


def _raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder,my-farm-advisor,openclaw",
            "AI_DEFAULT_API_KEY": "shared-ai-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
            "OPENCLAW_CHANNELS": "telegram",
        },
    )


def test_generated_litellm_virtual_keys_are_stable_across_rerun(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    install_env = tmp_path / ".install.env"
    install_env.write_text("STACK_NAME=wizard-stack\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    original_install_env = install_env.read_text(encoding="utf-8")

    first_keys = ensure_litellm_generated_keys(state_dir)
    second_keys = ensure_litellm_generated_keys(state_dir)

    assert first_keys == second_keys
    assert install_env.read_text(encoding="utf-8") == original_install_env

    raw_env = _raw_env()
    snapshot = cli._build_public_inspection_snapshot(
        raw_env=raw_env,
        desired_state=resolve_desired_state(raw_env),
        litellm_generated_keys=first_keys,
    )

    assert snapshot["litellm"]["master_key"] == "<redacted>"
    assert snapshot["litellm"]["salt_key"] == "<redacted>"
    assert snapshot["litellm"]["virtual_keys"] == {
        consumer: "<redacted>" for consumer in sorted(first_keys.virtual_keys)
    }

    serialized_snapshot = json.dumps(snapshot, sort_keys=True)
    for secret_value in (
        first_keys.master_key,
        first_keys.salt_key,
        *first_keys.virtual_keys.values(),
    ):
        assert secret_value not in serialized_snapshot


def test_empty_state_generates_consumer_keys_and_existing_state_reuses_them(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"

    generated_keys = ensure_litellm_generated_keys(state_dir)

    assert (state_dir / LITELLM_GENERATED_KEYS_FILE).exists()
    assert set(generated_keys.virtual_keys) == {
        "coder-hermes",
        "coder-kdense",
        "my-farm-advisor",
        "openclaw",
    }
    assert all(value for value in generated_keys.virtual_keys.values())

    existing_keys = LiteLLMGeneratedKeys(
        format_version=1,
        master_key="existing-master-key",
        salt_key="existing-salt-key",
        virtual_keys={
            "coder-hermes": "existing-coder-hermes-key",
            "coder-kdense": "existing-coder-kdense-key",
            "my-farm-advisor": "existing-my-farm-key",
            "openclaw": "existing-openclaw-key",
        },
    )
    write_litellm_generated_keys(state_dir, existing_keys)

    reused_keys = ensure_litellm_generated_keys(state_dir)

    assert reused_keys == existing_keys
