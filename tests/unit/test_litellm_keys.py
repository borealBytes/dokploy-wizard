# pyright: reportMissingImports=false

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.litellm.admin import (
    LiteLLMAdminError,
    LiteLLMGatewayManager,
    LiteLLMTeamRecord,
    LiteLLMVirtualKeyRecord,
)
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


class FakeLiteLLMAdminApi:
    def __init__(
        self,
        *,
        teams: tuple[LiteLLMTeamRecord, ...] = (),
        keys: tuple[LiteLLMVirtualKeyRecord, ...] = (),
        fail_create_key_for: str | None = None,
    ) -> None:
        self._teams = {team.team_alias: team for team in teams}
        self._keys = {key.key_alias: key for key in keys}
        self.fail_create_key_for = fail_create_key_for
        self.created_teams: list[LiteLLMTeamRecord] = []
        self.created_keys: list[LiteLLMVirtualKeyRecord] = []

    def readiness(self) -> dict[str, object]:
        return {"status": "connected", "db": "connected"}

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return tuple(self._teams.values())

    def create_team(self, *, team_alias: str, models: tuple[str, ...]) -> LiteLLMTeamRecord:
        team = LiteLLMTeamRecord(team_id=f"team-{team_alias}", team_alias=team_alias, models=models)
        self._teams[team_alias] = team
        self.created_teams.append(team)
        return team

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        return tuple(self._keys.values())

    def create_key(
        self,
        *,
        key: str,
        key_alias: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        del metadata
        if self.fail_create_key_for == key_alias:
            raise LiteLLMAdminError(f"failed to create key {key_alias}")
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
        )
        self._keys[key_alias] = record
        self.created_keys.append(record)
        return record

    def visible_models_for_key(self, key_alias: str) -> tuple[str, ...]:
        return self._keys[key_alias].models


def test_existing_virtual_key_is_reused_and_missing_key_is_created() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-my-farm-advisor",
                team_alias="my-farm-advisor",
                models=("local/unsloth-active", "openai/*", "openrouter/healer-alpha"),
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-my-farm-key",
                key_alias="my-farm-advisor",
                team_id="team-my-farm-advisor",
                models=("local/unsloth-active", "openai/*", "openrouter/healer-alpha"),
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={
            "my-farm-advisor": "existing-my-farm-key",
            "coder-kdense": "new-coder-kdense-key",
        },
        consumer_model_allowlists={
            "my-farm-advisor": ("local/unsloth-active", "openai/*", "openrouter/healer-alpha"),
            "coder-kdense": ("openai/*",),
        },
    )

    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="new-coder-kdense-key",
            key_alias="coder-kdense",
            team_id="team-coder-kdense",
            models=("openai/*",),
        )
    ]
    assert reconciled["my-farm-advisor"].key == "existing-my-farm-key"
    assert reconciled["coder-kdense"].key == "new-coder-kdense-key"
    assert api.visible_models_for_key("my-farm-advisor") == (
        "local/unsloth-active",
        "openai/*",
        "openrouter/healer-alpha",
    )
    assert api.visible_models_for_key("coder-kdense") == ("openai/*",)


def test_reconcile_virtual_keys_surfaces_key_creation_failures() -> None:
    api = FakeLiteLLMAdminApi(fail_create_key_for="coder-kdense")
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError) as error:
        manager.reconcile_virtual_keys(
            generated_keys={"coder-kdense": "new-coder-kdense-key"},
            consumer_model_allowlists={"coder-kdense": ("openai/*",)},
        )

    assert "failed to create key coder-kdense" in str(error.value)
