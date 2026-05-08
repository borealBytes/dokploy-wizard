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

_MANAGED_METADATA = {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"}


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
    assert generated_keys.master_key.startswith("sk-litellm-master-")
    assert generated_keys.salt_key.startswith("litellm-salt-")
    assert generated_keys.virtual_keys["coder-hermes"].startswith("sk-litellm-coder-hermes-")
    assert generated_keys.virtual_keys["coder-kdense"].startswith("sk-litellm-coder-kdense-")
    assert generated_keys.virtual_keys["my-farm-advisor"].startswith(
        "sk-litellm-my-farm-advisor-"
    )
    assert generated_keys.virtual_keys["openclaw"].startswith("sk-litellm-openclaw-")
    assert all(value for value in generated_keys.virtual_keys.values())

    existing_keys = LiteLLMGeneratedKeys(
        format_version=1,
        master_key="sk-litellm-master-existing",
        salt_key="existing-salt-key",
        virtual_keys={
            "coder-hermes": "sk-litellm-coder-hermes-existing",
            "coder-kdense": "sk-litellm-coder-kdense-existing",
            "my-farm-advisor": "sk-litellm-my-farm-advisor-existing",
            "openclaw": "sk-litellm-openclaw-existing",
        },
    )
    write_litellm_generated_keys(state_dir, existing_keys)

    reused_keys = ensure_litellm_generated_keys(state_dir)

    assert reused_keys == existing_keys


def test_ensure_litellm_generated_keys_repairs_legacy_non_sk_values(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    legacy_keys = LiteLLMGeneratedKeys(
        format_version=1,
        master_key="legacy-master-key",
        salt_key="existing-salt-key",
        virtual_keys={
            "coder-hermes": "sk-litellm-coder-hermes-existing",
            "coder-kdense": "legacy-coder-kdense-key",
            "my-farm-advisor": "345elegacy5043",
            "openclaw": "existing-openclaw-key",
        },
    )
    write_litellm_generated_keys(state_dir, legacy_keys)

    repaired_keys = ensure_litellm_generated_keys(state_dir)

    assert repaired_keys.master_key.startswith("sk-litellm-master-")
    assert repaired_keys.master_key != legacy_keys.master_key
    assert repaired_keys.salt_key == legacy_keys.salt_key
    assert repaired_keys.virtual_keys["coder-hermes"] == "sk-litellm-coder-hermes-existing"
    assert repaired_keys.virtual_keys["coder-kdense"].startswith("sk-litellm-coder-kdense-")
    assert repaired_keys.virtual_keys["coder-kdense"] != "legacy-coder-kdense-key"
    assert repaired_keys.virtual_keys["my-farm-advisor"].startswith(
        "sk-litellm-my-farm-advisor-"
    )
    assert repaired_keys.virtual_keys["my-farm-advisor"] != "345elegacy5043"
    assert repaired_keys.virtual_keys["openclaw"].startswith("sk-litellm-openclaw-")
    assert repaired_keys.virtual_keys["openclaw"] != "existing-openclaw-key"

    assert ensure_litellm_generated_keys(state_dir) == repaired_keys


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
        self.updated_teams: list[LiteLLMTeamRecord] = []
        self.updated_keys: list[LiteLLMVirtualKeyRecord] = []

    def readiness(self) -> dict[str, object]:
        return {"status": "connected", "db": "connected"}

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return tuple(self._teams.values())

    def create_team(
        self,
        *,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        team = LiteLLMTeamRecord(
            team_id=f"team-{team_alias}",
            team_alias=team_alias,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._teams[team_alias] = team
        self.created_teams.append(team)
        return team

    def update_team(
        self,
        *,
        team_id: str,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        team = LiteLLMTeamRecord(
            team_id=team_id,
            team_alias=team_alias,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._teams[team_alias] = team
        self.updated_teams.append(team)
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
        if self.fail_create_key_for == key_alias:
            raise LiteLLMAdminError(f"failed to create key {key_alias}")
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._keys[key_alias] = record
        self.created_keys.append(record)
        return record

    def update_key(
        self,
        *,
        key_alias: str,
        key: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
            metadata=dict(metadata or {}),
        )
        self._keys[key_alias] = record
        self.updated_keys.append(record)
        return record

    def visible_models_for_key(self, key_alias: str) -> tuple[str, ...]:
        return self._keys[key_alias].models


def test_existing_virtual_key_is_reused_and_missing_key_is_created() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-my-farm-advisor",
                team_alias="my-farm-advisor",
                models=(
                    "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                ),
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-my-farm-key",
                key_alias="my-farm-advisor",
                team_id="team-my-farm-advisor",
                models=(
                    "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                ),
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
            "my-farm-advisor": (
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            "coder-kdense": (
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
        },
    )

    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="new-coder-kdense-key",
            key_alias="coder-kdense",
            team_id="team-coder-kdense",
            models=(
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata={"consumer": "coder-kdense", "managed_by": "dokploy-wizard"},
        )
    ]
    assert reconciled["my-farm-advisor"].key == "existing-my-farm-key"
    assert reconciled["coder-kdense"].key == "new-coder-kdense-key"
    assert api.visible_models_for_key("my-farm-advisor") == (
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
        "openrouter/anthropic/claude-3.5-sonnet",
    )
    assert api.visible_models_for_key("coder-kdense") == (
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
        "openrouter/anthropic/claude-3.5-sonnet",
    )


def test_reconcile_virtual_keys_surfaces_key_creation_failures() -> None:
    api = FakeLiteLLMAdminApi(fail_create_key_for="coder-kdense")
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError) as error:
        manager.reconcile_virtual_keys(
            generated_keys={"coder-kdense": "new-coder-kdense-key"},
            consumer_model_allowlists={
                "coder-kdense": (
                    "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                )
            },
        )

    assert "failed to create key coder-kdense" in str(error.value)


def test_reconcile_virtual_keys_reuses_existing_key_when_generated_value_differs() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-my-farm-advisor",
                team_alias="my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="old-my-farm-key",
                key_alias="my-farm-advisor",
                team_id="team-my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"my-farm-advisor": "new-my-farm-key"},
        consumer_model_allowlists={
            "my-farm-advisor": ("tuxdesktop.tailb12aa5.ts.net/unsloth-active",)
        },
    )

    assert reconciled["my-farm-advisor"].key == "old-my-farm-key"
    assert api.visible_models_for_key("my-farm-advisor") == (
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
    )
    assert len(api.created_keys) == 0


def test_reconcile_virtual_keys_updates_managed_team_and_key_model_drift_without_rotating_key(
) -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-my-farm-advisor",
                team_alias="my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-my-farm-key",
                key_alias="my-farm-advisor",
                team_id="team-my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"my-farm-advisor": "new-my-farm-key"},
        consumer_model_allowlists={
            "my-farm-advisor": (
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            )
        },
    )

    assert api.updated_teams == [
        LiteLLMTeamRecord(
            team_id="team-my-farm-advisor",
            team_alias="my-farm-advisor",
            models=(
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata=_MANAGED_METADATA,
        )
    ]
    assert api.updated_keys == [
        LiteLLMVirtualKeyRecord(
            key="existing-my-farm-key",
            key_alias="my-farm-advisor",
            team_id="team-my-farm-advisor",
            models=(
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata=_MANAGED_METADATA,
        )
    ]
    assert reconciled["my-farm-advisor"].key == "existing-my-farm-key"


def test_reconcile_virtual_keys_fails_closed_for_unmanaged_team_model_drift() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-my-farm-advisor",
                team_alias="my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-my-farm-key",
                key_alias="my-farm-advisor",
                team_id="team-my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError, match="not wizard-managed"):
        manager.reconcile_virtual_keys(
            generated_keys={"my-farm-advisor": "existing-my-farm-key"},
            consumer_model_allowlists={
                "my-farm-advisor": (
                    "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                )
            },
        )


def test_reconcile_virtual_keys_fails_closed_for_mismatched_managed_key_metadata() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-my-farm-advisor",
                team_alias="my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-my-farm-key",
                key_alias="my-farm-advisor",
                team_id="team-my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
                metadata={"consumer": "openclaw", "managed_by": "dokploy-wizard"},
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError, match="belongs to consumer 'openclaw'"):
        manager.reconcile_virtual_keys(
            generated_keys={"my-farm-advisor": "existing-my-farm-key"},
            consumer_model_allowlists={
                "my-farm-advisor": (
                    "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                )
            },
        )
