# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

import dokploy_wizard.cli as cli
from dokploy_wizard.litellm.admin import (
    WORKSPACE_KEY_DENY_MODEL,
    WORKSPACE_KEY_GRANTED_MODELS,
    LiteLLMAdminError,
    LiteLLMGatewayManager,
    LiteLLMTeamRecord,
    LiteLLMVirtualKeyRecord,
    build_litellm_workspace_key_policy,
)
from dokploy_wizard.state import (
    LITELLM_GENERATED_KEYS_FILE,
    SEAWEEDFS_GENERATED_SECRETS_FILE,
    LiteLLMGeneratedKeys,
    RawEnvInput,
    SeaweedFsGeneratedSecrets,
    ensure_litellm_generated_keys,
    ensure_seaweedfs_generated_secrets,
    resolve_desired_state,
    write_litellm_generated_keys,
    write_seaweedfs_generated_secrets,
)
from dokploy_wizard.state.models import (
    LITELLM_WORKSPACE_ISSUER_CONSUMER,
    SEAWEEDFS_GENERATED_SECRET_PREFIXES,
)

_MANAGED_METADATA = {"consumer": "coder-hermes", "managed_by": "dokploy-wizard"}
_EXPECTED_LITELLM_CONSUMERS = {
    "coder-hermes",
    # Not a chat consumer: the scoped key every Coder workspace mints its own
    # child key with (see the template MINTPY block).
    "coder-workspace-issuer",
    "dokploy-ai",
    "nextcloud",
}
_EXPECTED_SEAWEEDFS_GENERATED_SECRET_NAMES = {"access_key", "secret_key"}


def _raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "wizard-stack",
            "ROOT_DOMAIN": "example.com",
            "PACKS": "coder,matrix",
            "AI_DEFAULT_API_KEY": "shared-ai-key",
            "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
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
    assert generated_keys.virtual_keys["nextcloud"].startswith("sk-litellm-nextcloud")
    assert generated_keys.master_key.startswith("sk-litellm-master-")
    assert generated_keys.salt_key.startswith("litellm-salt-")
    assert generated_keys.virtual_keys["coder-hermes"].startswith("sk-litellm-coder-hermes-")
    assert generated_keys.virtual_keys["dokploy-ai"].startswith("sk-litellm-dokploy-ai-")
    assert all(value for value in generated_keys.virtual_keys.values())

    existing_keys = LiteLLMGeneratedKeys(
        format_version=1,
        master_key="sk-<redacted-master-existing>",
        salt_key="existing-salt-key",
        virtual_keys={
            "coder-hermes": "sk-<redacted-coder-hermes-existing>",
            "dokploy-ai": "sk-<redacted-dokploy-ai-existing>",
        },
    )
    write_litellm_generated_keys(state_dir, existing_keys)

    reused_keys = ensure_litellm_generated_keys(state_dir)

    # Pre-nextcloud state reuses every existing value verbatim and only grows
    # the new consumer slot.
    assert reused_keys.master_key == existing_keys.master_key
    assert reused_keys.salt_key == existing_keys.salt_key
    for consumer, key in existing_keys.virtual_keys.items():
        assert reused_keys.virtual_keys[consumer] == key
    assert set(reused_keys.virtual_keys) == _EXPECTED_LITELLM_CONSUMERS
    assert reused_keys.virtual_keys["nextcloud"].startswith("sk-litellm-nextcloud-")


def test_ensure_litellm_generated_keys_repairs_legacy_non_sk_values(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    legacy_keys = LiteLLMGeneratedKeys(
        format_version=1,
        master_key="legacy-master-key",
        salt_key="existing-salt-key",
        virtual_keys={
            "coder-hermes": "sk-<redacted-coder-hermes-existing>",
            "nextcloud": "legacy-nextcloud-key",
            "dokploy-ai": "345elegacy5043",
        },
    )
    write_litellm_generated_keys(state_dir, legacy_keys)

    repaired_keys = ensure_litellm_generated_keys(state_dir)

    assert repaired_keys.master_key.startswith("sk-litellm-master-")
    assert repaired_keys.master_key != legacy_keys.master_key
    assert repaired_keys.salt_key == legacy_keys.salt_key
    assert repaired_keys.virtual_keys["coder-hermes"] == "sk-<redacted-coder-hermes-existing>"
    assert repaired_keys.virtual_keys["nextcloud"].startswith("sk-litellm-nextcloud-")
    assert repaired_keys.virtual_keys["nextcloud"] != "legacy-nextcloud-key"
    assert repaired_keys.virtual_keys["dokploy-ai"].startswith("sk-litellm-dokploy-ai-")
    assert repaired_keys.virtual_keys["dokploy-ai"] != "345elegacy5043"

    assert ensure_litellm_generated_keys(state_dir) == repaired_keys


def test_ensure_litellm_generated_keys_repairs_existing_state_missing_dokploy_ai_consumer(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    legacy_virtual_keys = {
        "coder-hermes": "sk-<redacted-coder-hermes-existing>",
        "coder-kdense": "sk-<redacted-coder-kdense-existing>",
        # State written by an older wizard carries consumer aliases the current
        # build no longer issues keys for; repair prunes them so ghost names
        # never reach the gateway key sync or the model-sync sidecar again.
        "retired-pack-consumer": "sk-<redacted-retired-pack-consumer-existing>",
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
    assert "retired-pack-consumer" not in repaired_keys.virtual_keys
    # A consumer retired with its template is pruned exactly like an unknown
    # ghost name: repair only ever converges on the current consumer list.
    assert "coder-kdense" not in repaired_keys.virtual_keys
    assert repaired_keys.master_key == legacy_payload["master_key"]
    assert repaired_keys.salt_key == legacy_payload["salt_key"]
    assert repaired_keys.virtual_keys["coder-hermes"] == legacy_virtual_keys["coder-hermes"]
    assert repaired_keys.virtual_keys["dokploy-ai"].startswith("sk-litellm-dokploy-ai-")


def test_seaweedfs_generated_s3_credentials_are_state_backed_and_repaired(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    install_env = tmp_path / ".install.env"
    install_env.write_text("STACK_NAME=wizard-stack\nROOT_DOMAIN=example.com\n", encoding="utf-8")
    original_install_env = install_env.read_text(encoding="utf-8")

    first_secrets = ensure_seaweedfs_generated_secrets(state_dir)
    second_secrets = ensure_seaweedfs_generated_secrets(state_dir)

    assert first_secrets == second_secrets
    assert install_env.read_text(encoding="utf-8") == original_install_env
    assert set(SEAWEEDFS_GENERATED_SECRET_PREFIXES) == _EXPECTED_SEAWEEDFS_GENERATED_SECRET_NAMES
    assert first_secrets.access_key.startswith(
        f"{SEAWEEDFS_GENERATED_SECRET_PREFIXES['access_key']}-"
    )
    assert first_secrets.secret_key.startswith(
        f"{SEAWEEDFS_GENERATED_SECRET_PREFIXES['secret_key']}-"
    )
    assert (state_dir / SEAWEEDFS_GENERATED_SECRETS_FILE).stat().st_mode & 0o777 == 0o600

    legacy_secrets = SeaweedFsGeneratedSecrets(
        format_version=1,
        access_key="seaweedfs-access-key-existing",
        secret_key="seaweedfs-secret-key-existing",
    )
    write_seaweedfs_generated_secrets(state_dir, legacy_secrets)

    repaired_secrets = ensure_seaweedfs_generated_secrets(state_dir)

    assert repaired_secrets == legacy_secrets
    assert install_env.read_text(encoding="utf-8") == original_install_env
    assert (state_dir / SEAWEEDFS_GENERATED_SECRETS_FILE).stat().st_mode & 0o777 == 0o600

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
        self.created_key_routes: dict[str, tuple[str, ...] | None] = {}
        self.deleted_team_ids: list[str] = []
        self.updated_key_routes: dict[str, tuple[str, ...] | None] = {}
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
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        if self.fail_create_key_for == key_alias:
            raise LiteLLMAdminError(f"failed to create key {key_alias}")
        self.created_key_routes[key_alias] = allowed_routes
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
        allowed_routes: tuple[str, ...] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
            metadata=dict(metadata or {}),
        )
        self.updated_key_routes[key_alias] = allowed_routes
        self._keys[key_alias] = record
        self.updated_keys.append(record)
        return record

    def delete_key(self, *, key_alias: str) -> None:
        self.deleted_key_aliases.append(key_alias)
        self._keys.pop(key_alias, None)

    def delete_team(self, *, team_id: str) -> None:
        self.deleted_team_ids.append(team_id)
        for alias, team in list(self._teams.items()):
            if team.team_id == team_id:
                del self._teams[alias]

    def visible_models_for_key(self, key_alias: str) -> tuple[str, ...]:
        return self._keys[key_alias].models


def test_existing_virtual_key_is_reused_and_missing_key_is_created() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=(
                    "local-model.internal/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                ),
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-coder-hermes-key",
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=(
                    "local-model.internal/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                ),
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={
            "coder-hermes": "existing-coder-hermes-key",
            "coder-kdense": "new-coder-kdense-key",
        },
        consumer_model_allowlists={
            "coder-hermes": (
                "local-model.internal/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            "coder-kdense": (
                "local-model.internal/unsloth-active",
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
                "local-model.internal/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata={"consumer": "coder-kdense", "managed_by": "dokploy-wizard"},
        )
    ]
    assert reconciled["coder-hermes"].key == "existing-coder-hermes-key"
    assert reconciled["coder-kdense"].key == "new-coder-kdense-key"
    assert api.visible_models_for_key("coder-hermes") == (
        "local-model.internal/unsloth-active",
        "openrouter/anthropic/claude-3.5-sonnet",
    )
    assert api.visible_models_for_key("coder-kdense") == (
        "local-model.internal/unsloth-active",
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
                    "local-model.internal/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                )
            },
        )

    assert "failed to create key coder-kdense" in str(error.value)


def test_reconcile_virtual_keys_creates_model_restricted_consumer_key_without_provider_keys(
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
        generated_keys={"coder-hermes": "sk-litellm-coder-hermes-test"},
        consumer_model_allowlists={
            "coder-hermes": (
                "openrouter/hunter-alpha",
                "openrouter/hunter-alpha",
                "local-model.internal/unsloth-active",
            )
        },
    )

    assert api.created_teams == [
        LiteLLMTeamRecord(
            team_id="team-coder-hermes",
            team_alias="coder-hermes",
            models=("openrouter/hunter-alpha", "local-model.internal/unsloth-active"),
            metadata={"consumer": "coder-hermes", "managed_by": "dokploy-wizard"},
        )
    ]
    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="sk-litellm-coder-hermes-test",
            key_alias="coder-hermes",
            team_id="team-coder-hermes",
            models=("openrouter/hunter-alpha", "local-model.internal/unsloth-active"),
            metadata={"consumer": "coder-hermes", "managed_by": "dokploy-wizard"},
        )
    ]
    assert reconciled["coder-hermes"].key == "sk-litellm-coder-hermes-test"
    serialized_contract = json.dumps(reconciled["coder-hermes"].__dict__, sort_keys=True)
    for provider_key in upstream_provider_values.values():
        assert provider_key not in serialized_contract


def test_reconcile_virtual_keys_recreates_managed_existing_key_when_generated_value_differs(
) -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="old-coder-hermes-key",
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": "new-coder-hermes-key"},
        consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",)
        },
    )

    assert api.deleted_key_aliases == ["coder-hermes"]
    assert api.updated_keys == []
    assert reconciled["coder-hermes"].key == "new-coder-hermes-key"
    assert api.visible_models_for_key("coder-hermes") == (
        "local-model.internal/unsloth-active",
    )
    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="new-coder-hermes-key",
            key_alias="coder-hermes",
            team_id="team-coder-hermes",
            models=("local-model.internal/unsloth-active",),
            metadata=_MANAGED_METADATA,
        )
    ]


def test_reconcile_virtual_keys_reuses_key_whose_hashed_token_matches_generated_value(
) -> None:
    """Live /key/list returns hashed material, never the raw key.

    Measured on the box: every wizard-managed alias comes back with
    ``token == sha256(raw)`` (and no raw key field at all), so the reconciler
    must prove the match through the hash. Before this, comparing the raw
    state value to the payload's ``key`` (which falls back to the hash) saw
    drift on every pass and churned delete+recreate for all four consumer
    keys — and the preserved-phase validator could never return true.
    """
    generated_key = "sk-coder-hermes-wizard-key"
    token_hash = hashlib.sha256(generated_key.encode()).hexdigest()
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key=token_hash,
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
                token=token_hash,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": generated_key},
        consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",)
        },
    )

    assert api.deleted_key_aliases == []
    assert api.created_keys == []
    assert api.updated_keys == []


def test_reconcile_virtual_keys_updates_managed_scope_drift_on_existing_accepted_key(
) -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="old-coder-hermes-key",
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": "new-coder-hermes-key"},
        consumer_model_allowlists={
            "coder-hermes": (
                "local-model.internal/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            )
        },
    )

    assert api.deleted_key_aliases == ["coder-hermes"]
    assert api.updated_keys == []
    assert api.created_keys == [
        LiteLLMVirtualKeyRecord(
            key="new-coder-hermes-key",
            key_alias="coder-hermes",
            team_id="team-coder-hermes",
            models=(
                "local-model.internal/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata=_MANAGED_METADATA,
        )
    ]
    assert reconciled["coder-hermes"].key == "new-coder-hermes-key"


def test_reconcile_virtual_keys_fails_closed_for_unmanaged_key_value_drift() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="old-coder-hermes-key",
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=("local-model.internal/unsloth-active",),
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError, match="not wizard-managed"):
        manager.reconcile_virtual_keys(
            generated_keys={"coder-hermes": "new-coder-hermes-key"},
            consumer_model_allowlists={
                "coder-hermes": ("local-model.internal/unsloth-active",)
            },
        )

    assert api.updated_keys == []
    assert api.deleted_key_aliases == []


def test_reconcile_virtual_keys_updates_managed_team_and_key_model_drift_without_rotating_key(
) -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-coder-hermes-key",
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": "existing-coder-hermes-key"},
        consumer_model_allowlists={
            "coder-hermes": (
                "local-model.internal/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            )
        },
    )

    assert api.updated_teams == [
        LiteLLMTeamRecord(
            team_id="team-coder-hermes",
            team_alias="coder-hermes",
            models=(
                "local-model.internal/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata=_MANAGED_METADATA,
        )
    ]
    assert api.updated_keys == [
        LiteLLMVirtualKeyRecord(
            key="existing-coder-hermes-key",
            key_alias="coder-hermes",
            team_id="team-coder-hermes",
            models=(
                "local-model.internal/unsloth-active",
                "openrouter/anthropic/claude-3.5-sonnet",
            ),
            metadata=_MANAGED_METADATA,
        )
    ]
    assert reconciled["coder-hermes"].key == "existing-coder-hermes-key"


def test_reconcile_virtual_keys_fails_closed_for_unmanaged_team_model_drift() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=("local-model.internal/unsloth-active",),
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-coder-hermes-key",
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError, match="not wizard-managed"):
        manager.reconcile_virtual_keys(
            generated_keys={"coder-hermes": "existing-coder-hermes-key"},
            consumer_model_allowlists={
                "coder-hermes": (
                    "local-model.internal/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                )
            },
        )


def test_reconcile_virtual_keys_fails_closed_for_mismatched_managed_key_metadata() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-hermes",
                team_alias="coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata=_MANAGED_METADATA,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="existing-coder-hermes-key",
                key_alias="coder-hermes",
                team_id="team-coder-hermes",
                models=("local-model.internal/unsloth-active",),
                metadata={"consumer": "coder-kdense", "managed_by": "dokploy-wizard"},
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    with pytest.raises(LiteLLMAdminError, match="belongs to consumer 'coder-kdense'"):
        manager.reconcile_virtual_keys(
            generated_keys={"coder-hermes": "existing-coder-hermes-key"},
            consumer_model_allowlists={
                "coder-hermes": (
                    "local-model.internal/unsloth-active",
                    "openrouter/anthropic/claude-3.5-sonnet",
                )
            },
        )


def test_reconcile_virtual_keys_limits_workspace_issuer_to_key_management_routes() -> None:
    api = FakeLiteLLMAdminApi()
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": "k-hermes", LITELLM_WORKSPACE_ISSUER_CONSUMER: "k-issuer"},
        consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",),
            LITELLM_WORKSPACE_ISSUER_CONSUMER: ("*",),
        },
    )

    assert reconciled[LITELLM_WORKSPACE_ISSUER_CONSUMER].key == "k-issuer"
    # Only the issuer gets the route fence; chat consumers keep the default
    # (inference) surface, and the issuer itself can mint, inspect, and revoke.
    assert api.created_key_routes[LITELLM_WORKSPACE_ISSUER_CONSUMER] == (
        "/key/generate",
        "/key/info",
        "/key/delete",
    )
    assert api.created_key_routes["coder-hermes"] is None
    assert reconciled[LITELLM_WORKSPACE_ISSUER_CONSUMER].models == ("*",)


def test_reconcile_virtual_keys_keeps_issuer_routes_on_scope_drift() -> None:
    issuer_metadata = {
        "consumer": LITELLM_WORKSPACE_ISSUER_CONSUMER,
        "managed_by": "dokploy-wizard",
    }
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-" + LITELLM_WORKSPACE_ISSUER_CONSUMER,
                team_alias=LITELLM_WORKSPACE_ISSUER_CONSUMER,
                models=("local-model.internal/unsloth-active",),
                metadata=issuer_metadata,
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="sk-litellm-coder-workspace-issuer-existing",
                key_alias=LITELLM_WORKSPACE_ISSUER_CONSUMER,
                team_id="team-" + LITELLM_WORKSPACE_ISSUER_CONSUMER,
                models=("local-model.internal/unsloth-active",),
                metadata=issuer_metadata,
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    manager.reconcile_virtual_keys(
        generated_keys={
            LITELLM_WORKSPACE_ISSUER_CONSUMER: "sk-litellm-coder-workspace-issuer-existing"
        },
        consumer_model_allowlists={LITELLM_WORKSPACE_ISSUER_CONSUMER: ("*",)},
    )

    assert api.updated_key_routes[LITELLM_WORKSPACE_ISSUER_CONSUMER] == (
        "/key/generate",
        "/key/info",
        "/key/delete",
    )


def test_reconcile_virtual_keys_prunes_retired_managed_consumers() -> None:
    api = FakeLiteLLMAdminApi(
        teams=(
            LiteLLMTeamRecord(
                team_id="team-coder-kdense",
                team_alias="coder-kdense",
                models=("local-model.internal/unsloth-active",),
                metadata={"consumer": "coder-kdense", "managed_by": "dokploy-wizard"},
            ),
            LiteLLMTeamRecord(
                team_id="team-operator-team",
                team_alias="operator-team",
                models=("*",),
                metadata={"owned_by": "operator"},
            ),
        ),
        keys=(
            LiteLLMVirtualKeyRecord(
                key="sk-retired-kdense",
                key_alias="coder-kdense",
                team_id="team-coder-kdense",
                models=("local-model.internal/unsloth-active",),
                metadata={"consumer": "coder-kdense", "managed_by": "dokploy-wizard"},
            ),
            LiteLLMVirtualKeyRecord(
                key="sk-operator-key",
                key_alias="operator-key",
                team_id=None,
                models=("*",),
                metadata={},
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    reconciled = manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": "k-hermes"},
        consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",),
        },
    )

    assert set(reconciled) == {"coder-hermes"}
    # The retired wizard-managed consumer is swept end to end...
    assert api.deleted_key_aliases == ["coder-kdense"]
    assert api.deleted_team_ids == ["team-coder-kdense"]
    # ...while records without the wizard marker belong to the operator and stay.
    assert api.list_keys() and any(record.key_alias == "operator-key" for record in api.list_keys())
    assert any(team.team_alias == "operator-team" for team in api.list_teams())


def test_reconcile_virtual_keys_spare_workspace_minted_keys() -> None:
    """Workspace-minted keys are wizard-marked but not state consumers.

    The template MINTPY lane mints ``<login>-<workspace>`` aliases carrying
    ``coder_workspace`` metadata; the alias never appears in the wizard's
    generated-keys document. Before the exemption, the retired-consumer sweep
    deleted every live workspace key on every real shared_core pass (measured
    on the box: /key/list held exactly the four state aliases after a pass,
    the workspace key gone). MINTPY re-mints at next boot, so the box only
    showed silent per-pass churn — the exemption must keep the key.
    """
    api = FakeLiteLLMAdminApi(
        keys=(
            LiteLLMVirtualKeyRecord(
                key="sk-clayton-dev",
                key_alias="clayton-dev",
                team_id=None,
                models=("sync/*", "sync/default-chat"),
                metadata={
                    "managed_by": "dokploy-wizard",
                    "coder_workspace": "dev",
                    "coder_owner": "clayton",
                },
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": "k-hermes"},
        consumer_model_allowlists={
            "coder-hermes": ("local-model.internal/unsloth-active",),
        },
    )

    assert api.deleted_key_aliases == []
    assert any(record.key_alias == "clayton-dev" for record in api.list_keys())


def _minted_workspace_key(
    *,
    alias: str,
    owner: str | None,
    workspace: str,
    models: tuple[str, ...],
    key: str,
) -> LiteLLMVirtualKeyRecord:
    metadata: dict[str, object] = {
        "managed_by": "dokploy-wizard",
        "coder_workspace": workspace,
    }
    if owner is not None:
        metadata["coder_owner"] = owner
    return LiteLLMVirtualKeyRecord(
        key=key,
        key_alias=alias,
        team_id=None,
        models=models,
        metadata=metadata,
    )


def test_workspace_key_policy_sweep_degrades_out_of_policy_mint_without_reboot() -> None:
    """A policy flip re-scopes live workspace keys admin-side, no boot needed.

    The minted token is deterministic (sha256 of issuer + ``<login>-<workspace>``),
    so the master-key sweep can address the raw token and /key/update the grant
    on any shared_core pass. The key is spared by the retired sweep (it stays)
    and updated in place; a second pass proves the re-scope converged.
    """
    issuer = "sk-litellm-coder-workspace-issuer-generated"
    api = FakeLiteLLMAdminApi(
        keys=(
            _minted_workspace_key(
                alias="guest-dev",
                owner="guest",
                workspace="dev",
                models=WORKSPACE_KEY_GRANTED_MODELS,
                key="sk-stored-mint-material",
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)
    policy = build_litellm_workspace_key_policy(
        allow_new_users=False, allowed_users=(), admin_email="Clayton@Example.com"
    )

    manager.reconcile_virtual_keys(
        generated_keys={LITELLM_WORKSPACE_ISSUER_CONSUMER: issuer},
        consumer_model_allowlists={LITELLM_WORKSPACE_ISSUER_CONSUMER: ("sync/*",)},
        workspace_key_policy=policy,
    )

    assert api.deleted_key_aliases == []
    assert len(api.updated_keys) == 1
    updated = api.updated_keys[0]
    assert updated.key_alias == "guest-dev"
    assert updated.models == (WORKSPACE_KEY_DENY_MODEL,)
    assert updated.key == (
        "sk-" + hashlib.sha256((issuer + ":guest-dev").encode()).hexdigest()[:40]
    )

    # Idempotent: the degraded record now matches the deny policy, so the
    # next shared_core pass changes nothing.
    manager.reconcile_virtual_keys(
        generated_keys={LITELLM_WORKSPACE_ISSUER_CONSUMER: issuer},
        consumer_model_allowlists={LITELLM_WORKSPACE_ISSUER_CONSUMER: ("sync/*",)},
        workspace_key_policy=policy,
    )
    assert len(api.updated_keys) == 1


def test_workspace_key_policy_sweep_upgrades_allowlisted_owner() -> None:
    issuer = "sk-litellm-coder-workspace-issuer-generated"
    api = FakeLiteLLMAdminApi(
        keys=(
            _minted_workspace_key(
                alias="bob-dev",
                owner="bob",
                workspace="dev",
                models=(WORKSPACE_KEY_DENY_MODEL,),
                key="sk-bob-mint",
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    manager.reconcile_virtual_keys(
        generated_keys={LITELLM_WORKSPACE_ISSUER_CONSUMER: issuer},
        consumer_model_allowlists={LITELLM_WORKSPACE_ISSUER_CONSUMER: ("sync/*",)},
        workspace_key_policy=build_litellm_workspace_key_policy(
            allow_new_users=False,
            # Allowlist logins normalize to lowercase: Coder logins are
            # lowercase by derivation, and this comparison must match.
            allowed_users=("Bob",),
            admin_email="admin@example.com",
        ),
    )

    assert api.deleted_key_aliases == []
    assert [record.models for record in api.updated_keys] == [WORKSPACE_KEY_GRANTED_MODELS]


def test_workspace_key_policy_sweep_spares_exempt_unattributed_and_foreign_keys() -> None:
    """Admin exemption, anonymous spares, and operator-owned keys all stay put."""
    issuer = "sk-litellm-coder-workspace-issuer-generated"
    api = FakeLiteLLMAdminApi(
        keys=(
            _minted_workspace_key(
                alias="clayton-dev",
                owner="clayton",
                workspace="dev",
                models=WORKSPACE_KEY_GRANTED_MODELS,
                key="sk-admin-mint",
            ),
            _minted_workspace_key(
                alias="orphan-dev",
                owner=None,
                workspace="dev",
                models=WORKSPACE_KEY_GRANTED_MODELS,
                key="sk-orphan-mint",
            ),
            LiteLLMVirtualKeyRecord(
                key="sk-operator",
                key_alias="operator-mint",
                team_id=None,
                models=("*",),
                metadata={},
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    manager.reconcile_virtual_keys(
        generated_keys={LITELLM_WORKSPACE_ISSUER_CONSUMER: issuer},
        consumer_model_allowlists={LITELLM_WORKSPACE_ISSUER_CONSUMER: ("sync/*",)},
        workspace_key_policy=build_litellm_workspace_key_policy(
            allow_new_users=False, allowed_users=(), admin_email="clayton@example.com"
        ),
    )

    # The seeded admin is always exempt (granted, no update needed); the
    # owner-less mint is spared rather than guessed at; the unmarked operator
    # key is invisible to the sweep and to the retired sweep.
    assert api.updated_keys == []
    assert api.deleted_key_aliases == []
    assert api.deleted_team_ids == []


def test_workspace_key_policy_sweep_is_inert_without_issuer_material() -> None:
    """No issuer raw in state means the deterministic token cannot be re-derived."""
    api = FakeLiteLLMAdminApi(
        keys=(
            _minted_workspace_key(
                alias="guest-dev",
                owner="guest",
                workspace="dev",
                models=WORKSPACE_KEY_GRANTED_MODELS,
                key="sk-guest-mint",
            ),
        ),
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)

    manager.reconcile_virtual_keys(
        generated_keys={"coder-hermes": "k-hermes"},
        consumer_model_allowlists={"coder-hermes": ("sync/*",)},
        workspace_key_policy=build_litellm_workspace_key_policy(
            allow_new_users=False, allowed_users=(), admin_email="admin@example.com"
        ),
    )

    assert api.updated_keys == []
    assert api.deleted_key_aliases == []
