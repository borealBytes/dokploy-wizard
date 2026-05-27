# pyright: reportMissingImports=false

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

import dokploy_wizard.cli as cli
from dokploy_wizard.litellm.admin import (
    LiteLLMAdminError,
    LiteLLMGatewayManager,
    LiteLLMTeamRecord,
    LiteLLMVirtualKeyRecord,
)
from dokploy_wizard.state import (
    LITELLM_GENERATED_KEYS_FILE,
    SURFSENSE_GENERATED_SECRETS_FILE,
    LiteLLMGeneratedKeys,
    RawEnvInput,
    SurfSenseGeneratedSecrets,
    ensure_litellm_generated_keys,
    ensure_surfsense_generated_secrets,
    resolve_desired_state,
    write_litellm_generated_keys,
    write_surfsense_generated_secrets,
)
from dokploy_wizard.state.models import SURFSENSE_GENERATED_SECRET_PREFIXES

_MANAGED_METADATA = {"consumer": "my-farm-advisor", "managed_by": "dokploy-wizard"}
_EXPECTED_LITELLM_CONSUMERS = {
    "coder-hermes",
    "coder-kdense",
    "dokploy-ai",
    "my-farm-advisor",
    "openclaw",
    "surfsense",
}
_EXPECTED_SURFSENSE_GENERATED_SECRET_NAMES = {
    "db_password",
    "jwt_secret",
    "searxng_secret",
    "secret_key",
    "zero_admin_password",
}


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
        consumer: "<redacted>" for consumer in sorted(_EXPECTED_LITELLM_CONSUMERS)
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
    assert set(generated_keys.virtual_keys) == _EXPECTED_LITELLM_CONSUMERS
    assert generated_keys.master_key.startswith("sk-litellm-master-")
    assert generated_keys.salt_key.startswith("litellm-salt-")
    assert generated_keys.virtual_keys["coder-hermes"].startswith("sk-litellm-coder-hermes-")
    assert generated_keys.virtual_keys["coder-kdense"].startswith("sk-litellm-coder-kdense-")
    assert generated_keys.virtual_keys["dokploy-ai"].startswith("sk-litellm-dokploy-ai-")
    assert generated_keys.virtual_keys["my-farm-advisor"].startswith(
        "sk-litellm-my-farm-advisor-"
    )
    assert generated_keys.virtual_keys["openclaw"].startswith("sk-litellm-openclaw-")
    assert generated_keys.virtual_keys["surfsense"].startswith("sk-litellm-surfsense-")
    assert all(value for value in generated_keys.virtual_keys.values())

    existing_keys = LiteLLMGeneratedKeys(
        format_version=1,
        master_key="sk-<redacted-master-existing>",
        salt_key="existing-salt-key",
        virtual_keys={
            "coder-hermes": "sk-<redacted-coder-hermes-existing>",
            "coder-kdense": "sk-<redacted-coder-kdense-existing>",
            "dokploy-ai": "sk-<redacted-dokploy-ai-existing>",
            "my-farm-advisor": "sk-<redacted-my-farm-advisor-existing>",
            "openclaw": "sk-<redacted-openclaw-existing>",
            "surfsense": "sk-<redacted-surfsense-existing>",
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
            "coder-hermes": "sk-<redacted-coder-hermes-existing>",
            "coder-kdense": "legacy-coder-kdense-key",
            "dokploy-ai": "legacy-dokploy-ai-key",
            "my-farm-advisor": "345elegacy5043",
            "openclaw": "existing-openclaw-key",
            "surfsense": "legacy-surfsense-key",
        },
    )
    write_litellm_generated_keys(state_dir, legacy_keys)

    repaired_keys = ensure_litellm_generated_keys(state_dir)

    assert repaired_keys.master_key.startswith("sk-litellm-master-")
    assert repaired_keys.master_key != legacy_keys.master_key
    assert repaired_keys.salt_key == legacy_keys.salt_key
    assert repaired_keys.virtual_keys["coder-hermes"] == "sk-<redacted-coder-hermes-existing>"
    assert repaired_keys.virtual_keys["coder-kdense"].startswith("sk-litellm-coder-kdense-")
    assert repaired_keys.virtual_keys["coder-kdense"] != "legacy-coder-kdense-key"
    assert repaired_keys.virtual_keys["dokploy-ai"].startswith("sk-litellm-dokploy-ai-")
    assert repaired_keys.virtual_keys["dokploy-ai"] != "legacy-dokploy-ai-key"
    assert repaired_keys.virtual_keys["my-farm-advisor"].startswith(
        "sk-litellm-my-farm-advisor-"
    )
    assert repaired_keys.virtual_keys["my-farm-advisor"] != "345elegacy5043"
    assert repaired_keys.virtual_keys["openclaw"].startswith("sk-litellm-openclaw-")
    assert repaired_keys.virtual_keys["openclaw"] != "existing-openclaw-key"
    assert repaired_keys.virtual_keys["surfsense"].startswith("sk-litellm-surfsense-")
    assert repaired_keys.virtual_keys["surfsense"] != "legacy-surfsense-key"

    assert ensure_litellm_generated_keys(state_dir) == repaired_keys


def test_ensure_litellm_generated_keys_repairs_existing_state_missing_dokploy_ai_consumer(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    legacy_virtual_keys = {
        "coder-hermes": "sk-<redacted-coder-hermes-existing>",
        "coder-kdense": "sk-<redacted-coder-kdense-existing>",
        "my-farm-advisor": "sk-<redacted-my-farm-advisor-existing>",
        "openclaw": "sk-<redacted-openclaw-existing>",
    }
    legacy_payload = {
        "format_version": 1,
        "master_key": "sk-<redacted-master-existing>",
        "salt_key": "existing-salt-key",
        "virtual_keys": legacy_virtual_keys,
    }
    (state_dir / LITELLM_GENERATED_KEYS_FILE).write_text(
        json.dumps(legacy_payload, sort_keys=True),
        encoding="utf-8",
    )

    repaired_keys = ensure_litellm_generated_keys(state_dir)

    assert set(repaired_keys.virtual_keys) == _EXPECTED_LITELLM_CONSUMERS
    assert repaired_keys.master_key == legacy_payload["master_key"]
    assert repaired_keys.salt_key == legacy_payload["salt_key"]
    for consumer in ("coder-hermes", "coder-kdense", "my-farm-advisor", "openclaw"):
        assert repaired_keys.virtual_keys[consumer] == legacy_virtual_keys[consumer]
    assert repaired_keys.virtual_keys["dokploy-ai"].startswith("sk-litellm-dokploy-ai-")
    assert repaired_keys.virtual_keys["surfsense"].startswith("sk-litellm-surfsense-")


def test_surfsense_generated_app_secrets_are_state_backed_and_repaired(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    install_env = tmp_path / ".install.env"
    install_env.write_text("STACK_NAME=wizard-stack\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    original_install_env = install_env.read_text(encoding="utf-8")

    first_secrets = ensure_surfsense_generated_secrets(state_dir)
    second_secrets = ensure_surfsense_generated_secrets(state_dir)

    assert first_secrets == second_secrets
    assert install_env.read_text(encoding="utf-8") == original_install_env
    assert set(SURFSENSE_GENERATED_SECRET_PREFIXES) == _EXPECTED_SURFSENSE_GENERATED_SECRET_NAMES
    assert set(first_secrets.secrets) == _EXPECTED_SURFSENSE_GENERATED_SECRET_NAMES
    for secret_name, prefix in SURFSENSE_GENERATED_SECRET_PREFIXES.items():
        assert first_secrets.secrets[secret_name].startswith(f"{prefix}-")
    assert (state_dir / SURFSENSE_GENERATED_SECRETS_FILE).stat().st_mode & 0o777 == 0o600

    legacy_secrets = SurfSenseGeneratedSecrets(
        format_version=1,
        secrets={
            "db_password": "surfsense-db-password-existing",
            "secret_key": "surfsense-secret-key-existing",
        },
    )
    write_surfsense_generated_secrets(state_dir, legacy_secrets)

    repaired_secrets = ensure_surfsense_generated_secrets(state_dir)

    assert set(repaired_secrets.secrets) == _EXPECTED_SURFSENSE_GENERATED_SECRET_NAMES
    assert repaired_secrets.secrets["db_password"] == "surfsense-db-password-existing"
    assert repaired_secrets.secrets["secret_key"] == "surfsense-secret-key-existing"
    assert repaired_secrets.secrets["jwt_secret"].startswith("surfsense-jwt-secret-")
    assert repaired_secrets.secrets["searxng_secret"].startswith("surfsense-searxng-secret-")
    assert repaired_secrets.secrets["zero_admin_password"].startswith(
        "surfsense-zero-admin-password-"
    )
    assert install_env.read_text(encoding="utf-8") == original_install_env
    assert (state_dir / SURFSENSE_GENERATED_SECRETS_FILE).stat().st_mode & 0o777 == 0o600


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
        self.deleted_key_aliases: list[str] = []

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

    def delete_key(self, *, key_alias: str) -> None:
        self.deleted_key_aliases.append(key_alias)
        self._keys.pop(key_alias, None)

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


def test_reconcile_virtual_keys_creates_model_restricted_surfsense_key_without_provider_keys(
) -> None:
    api = FakeLiteLLMAdminApi()
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)
    upstream_provider_values = {
        "LITELLM_OPENROUTER_API_KEY": "sk-openrouter-upstream",
        "LITELLM_LOCAL_API_KEY": "sk-local-upstream",
        "OPENROUTER_API_KEY": "sk-legacy-openrouter-upstream",
        "AI_DEFAULT_API_KEY": "sk-ai-default-upstream",
    }

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"surfsense": "sk-litellm-surfsense-test"},
        consumer_model_allowlists={
            "surfsense": (
                "openrouter/hunter-alpha",
                "openrouter/hunter-alpha",
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
            )
        },
    )

    assert api.created_teams == [
        LiteLLMTeamRecord(
            team_id="team-surfsense",
            team_alias="surfsense",
            models=("openrouter/hunter-alpha", "tuxdesktop.tailb12aa5.ts.net/unsloth-active"),
            metadata={"consumer": "surfsense", "managed_by": "dokploy-wizard"},
        )
    ]
    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="sk-litellm-surfsense-test",
            key_alias="surfsense",
            team_id="team-surfsense",
            models=("openrouter/hunter-alpha", "tuxdesktop.tailb12aa5.ts.net/unsloth-active"),
            metadata={"consumer": "surfsense", "managed_by": "dokploy-wizard"},
        )
    ]
    assert reconciled["surfsense"].key == "sk-litellm-surfsense-test"
    serialized_contract = json.dumps(reconciled["surfsense"].__dict__, sort_keys=True)
    for provider_key in upstream_provider_values.values():
        assert provider_key not in serialized_contract


def test_reconcile_virtual_keys_recreates_managed_existing_key_when_generated_value_differs(
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
                key="old-my-farm-key",
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
            "my-farm-advisor": ("tuxdesktop.tailb12aa5.ts.net/unsloth-active",)
        },
    )

    assert api.deleted_key_aliases == ["my-farm-advisor"]
    assert api.updated_keys == []
    assert reconciled["my-farm-advisor"].key == "new-my-farm-key"
    assert api.visible_models_for_key("my-farm-advisor") == (
        "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
    )
    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="new-my-farm-key",
            key_alias="my-farm-advisor",
            team_id="team-my-farm-advisor",
            models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
            metadata=_MANAGED_METADATA,
        )
    ]


def test_reconcile_virtual_keys_updates_managed_scope_drift_on_existing_accepted_key(
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
                key="old-my-farm-key",
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

    assert api.deleted_key_aliases == ["my-farm-advisor"]
    assert api.updated_keys == []
    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="new-my-farm-key",
            key_alias="my-farm-advisor",
            team_id="team-my-farm-advisor",
            models=(
                "tuxdesktop.tailb12aa5.ts.net/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata=_MANAGED_METADATA,
        )
    ]
    assert reconciled["my-farm-advisor"].key == "new-my-farm-key"


def test_reconcile_virtual_keys_fails_closed_for_unmanaged_key_value_drift() -> None:
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
                key="old-my-farm-key",
                key_alias="my-farm-advisor",
                team_id="team-my-farm-advisor",
                models=("tuxdesktop.tailb12aa5.ts.net/unsloth-active",),
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError, match="not wizard-managed"):
        manager.reconcile_virtual_keys(
            generated_keys={"my-farm-advisor": "new-my-farm-key"},
            consumer_model_allowlists={
                "my-farm-advisor": ("tuxdesktop.tailb12aa5.ts.net/unsloth-active",)
            },
        )

    assert api.updated_keys == []
    assert api.deleted_key_aliases == []


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
        generated_keys={"my-farm-advisor": "existing-my-farm-key"},
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
