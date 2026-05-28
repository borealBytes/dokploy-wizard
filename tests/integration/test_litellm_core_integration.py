# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

import dokploy_wizard.cli
from dokploy_wizard.cli import run_install_flow, run_modify_flow
from dokploy_wizard.core.planner import build_shared_core_plan
from dokploy_wizard.dokploy import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.shared_core import (
    DokploySharedCoreBackend,
    build_litellm_consumer_model_allowlists,
)
from dokploy_wizard.litellm import build_litellm_config
from dokploy_wizard.litellm.admin import LiteLLMTeamRecord, LiteLLMVirtualKeyRecord
from dokploy_wizard.state import (
    RawEnvInput,
    load_litellm_generated_keys,
    load_state_dir,
    resolve_desired_state,
)
from dokploy_wizard.state.models import LiteLLMGeneratedKeys
from tests.integration.test_networking_reconciler import (
    FakeCloudflareBackend as NetworkingCloudflareBackend,
)
from tests.integration.test_networking_reconciler import (
    FakeCoderBackend,
)
from tests.integration.test_openclaw_pack import (
    FakeCloudflareBackend,
    FakeDokployBackend,
    FakeHeadscaleBackend,
    FakeMatrixBackend,
    FakeOpenClawBackend,
    FakeSharedCoreBackend,
    RecordingDokployOpenClawApi,
    _base_install_values,
    _patch_real_dokploy_openclaw_backend,
)

_EXPECTED_OPENCODE_GO_CHAT_ALIASES = (
    "opencode-go/minimax-m2.7",
    "opencode-go/minimax-m2.5",
    "opencode-go/kimi-k2.6",
    "opencode-go/kimi-k2.5",
    "opencode-go/glm-5.1",
    "opencode-go/glm-5",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/deepseek-v4-flash",
    "opencode-go/qwen3.6-plus",
    "opencode-go/qwen3.5-plus",
    "opencode-go/mimo-v2-pro",
    "opencode-go/mimo-v2-omni",
    "opencode-go/mimo-v2.5-pro",
    "opencode-go/mimo-v2.5",
)


@dataclass
class RecordingDokploySharedCoreApi:
    project_name: str | None = None
    created_project: DokployCreatedProject = field(
        default_factory=lambda: DokployCreatedProject(project_id="proj-1", environment_id="env-1")
    )
    compose_names_by_id: dict[str, str] = field(default_factory=dict)
    compose_files_by_name: dict[str, str] = field(default_factory=dict)
    create_project_calls: int = 0
    create_compose_calls: int = 0
    update_compose_calls: int = 0
    deploy_calls: int = 0

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        if self.project_name is None:
            return ()
        environment = DokployEnvironmentSummary(
            environment_id=self.created_project.environment_id,
            name="default",
            is_default=True,
            composes=tuple(
                DokployComposeSummary(compose_id=compose_id, name=name, status="done")
                for compose_id, name in self.compose_names_by_id.items()
            ),
        )
        return (
            DokployProjectSummary(
                project_id=self.created_project.project_id,
                name=self.project_name,
                environments=(environment,),
            ),
        )

    def create_project(self, *, name: str, description: str | None, env: str | None) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        self.project_name = name
        return self.created_project

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del environment_id, app_name
        self.create_compose_calls += 1
        compose_id = f"compose-{len(self.compose_names_by_id) + 1}"
        self.compose_names_by_id[compose_id] = name
        self.compose_files_by_name[name] = compose_file
        return DokployComposeRecord(compose_id=compose_id, name=name)

    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord:
        del env
        name = self.compose_names_by_id[compose_id]
        if compose_file is not None:
            self.update_compose_calls += 1
            self.compose_files_by_name[name] = compose_file
        return DokployComposeRecord(compose_id=compose_id, name=name)

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message=None)


@dataclass
class RecordingManagedDriftLiteLLMAdminApi:
    teams: dict[str, LiteLLMTeamRecord]
    keys: dict[str, LiteLLMVirtualKeyRecord]
    update_team_calls: list[dict[str, object]] = field(default_factory=list)
    update_key_calls: list[dict[str, object]] = field(default_factory=list)
    create_key_calls: list[dict[str, object]] = field(default_factory=list)
    delete_key_calls: list[dict[str, object]] = field(default_factory=list)

    def readiness(self) -> dict[str, object]:
        return {"status": "connected", "db": "connected"}

    def list_teams(self) -> tuple[LiteLLMTeamRecord, ...]:
        return tuple(self.teams.values())

    def create_team(
        self,
        *,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        record = LiteLLMTeamRecord(
            team_id=f"team-{team_alias}",
            team_alias=team_alias,
            models=models,
            metadata=metadata or {},
        )
        self.teams[team_alias] = record
        return record

    def update_team(
        self,
        *,
        team_id: str,
        team_alias: str,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMTeamRecord:
        self.update_team_calls.append(
            {
                "team_id": team_id,
                "team_alias": team_alias,
                "models": models,
                "metadata": dict(metadata or {}),
            }
        )
        record = LiteLLMTeamRecord(
            team_id=team_id,
            team_alias=team_alias,
            models=models,
            metadata=metadata or {},
        )
        self.teams[team_alias] = record
        return record

    def list_keys(self) -> tuple[LiteLLMVirtualKeyRecord, ...]:
        return tuple(self.keys.values())

    def create_key(
        self,
        *,
        key: str,
        key_alias: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        self.create_key_calls.append(
            {
                "key": key,
                "key_alias": key_alias,
                "team_id": team_id,
                "models": models,
                "metadata": dict(metadata or {}),
            }
        )
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
            metadata=metadata or {},
        )
        self.keys[key_alias] = record
        return record

    def delete_key(self, *, key_alias: str) -> None:
        self.delete_key_calls.append({"key_alias": key_alias})
        self.keys.pop(key_alias, None)

    def update_key(
        self,
        *,
        key_alias: str,
        key: str,
        team_id: str | None,
        models: tuple[str, ...],
        metadata: Mapping[str, object] | None = None,
    ) -> LiteLLMVirtualKeyRecord:
        self.update_key_calls.append(
            {
                "key_alias": key_alias,
                "key": key,
                "team_id": team_id,
                "models": models,
                "metadata": dict(metadata or {}),
            }
        )
        record = LiteLLMVirtualKeyRecord(
            key=key,
            key_alias=key_alias,
            team_id=team_id,
            models=models,
            metadata=metadata or {},
        )
        self.keys[key_alias] = record
        return record


def _core_only_raw_env(**overrides: str) -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values=_base_install_values(PACKS="", **overrides),
    )


def _litellm_shared_core_backend(
    raw_env: RawEnvInput,
    client: RecordingDokploySharedCoreApi,
    *,
    state_dir: Path,
) -> DokploySharedCoreBackend:
    desired_state = resolve_desired_state(raw_env)
    backend = DokploySharedCoreBackend(
        api_url=desired_state.dokploy_api_url or "https://dokploy.example.com/api",
        api_key=raw_env.values["DOKPLOY_API_KEY"],
        stack_name=desired_state.stack_name,
        plan=desired_state.shared_core,
        litellm_env=dict(raw_env.values),
        litellm_consumer_model_allowlists=build_litellm_consumer_model_allowlists(
            flat_env=dict(raw_env.values),
            plan=desired_state.shared_core,
        ),
        allocation_provisioner=lambda allocations: None,
        client=client,
        sleep_fn=lambda _: None,
        state_dir=state_dir,
    )
    setattr(backend, "_wait_for_shared_core_containers", lambda: None)
    return backend


def _litellm_route_env(**overrides: str) -> dict[str, str]:
    values = {
        "LITELLM_LOCAL_BASE_URL": "http://local-model.internal:61434/v1",
        "LITELLM_LOCAL_MODEL": "unsloth-active",
        "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go/v1",
        "LITELLM_OPENCODE_GO_API_KEY": "sk-opencode-go-key",
        "LITELLM_OPENROUTER_API_KEY": "sk-openrouter-key",
        "LITELLM_OPENROUTER_MODELS": (
            "anthropic/claude-3.7-sonnet,"
            "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini"
        ),
        "LITELLM_NVIDIA_MODELS": "nvidia/kimi-k2.5=nvidia/moonshotai/kimi-k2.5",
        "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "NVIDIA_API_KEY": "sk-nvidia-key",
    }
    values.update(overrides)
    return values


def _model_names(config: dict[str, object]) -> list[str]:
    model_list = cast(list[dict[str, Any]], config["model_list"])
    return [str(entry["model_name"]) for entry in model_list]


def _generated_keys_for_consumers(consumers: tuple[str, ...]) -> LiteLLMGeneratedKeys:
    return LiteLLMGeneratedKeys(
        format_version=1,
        master_key="sk-master-generated",
        salt_key="sk-salt-generated",
        virtual_keys={consumer: f"sk-generated-{consumer}" for consumer in consumers},
    )


def test_build_litellm_config_integrates_all_supported_provider_types() -> None:
    config = build_litellm_config(
        _litellm_route_env(),
        {
            "nvidia_api_key_env": "NVIDIA_API_KEY",
            "openrouter_model_metadata": {
                "anthropic/claude-3.7-sonnet": {
                    "pricing": {"prompt": "0.000003", "completion": "0.000015"}
                },
                "openrouter/openai/gpt-4.1-mini": {
                    "pricing": {"prompt": "0.0000008", "completion": "0.0000032"}
                },
            }
        },
    )

    assert _model_names(config) == [
        "local-model.internal/unsloth-active",
        *_EXPECTED_OPENCODE_GO_CHAT_ALIASES,
        "openrouter/anthropic/claude-3.7-sonnet",
        "nvidia/kimi-k2.5",
        "openrouter/hunter-alpha",
    ]

    entries = {
        entry["model_name"]: entry
        for entry in cast(list[dict[str, Any]], config["model_list"])
    }
    assert entries["local-model.internal/unsloth-active"]["litellm_params"] == {
        "model": "openai/unsloth-active",
        "api_base": "http://local-model.internal:61434/v1",
        "api_key": "sk-no-key-required",
    }
    assert entries["opencode-go/minimax-m2.7"]["litellm_params"] == {
        "model": "openai/minimax-m2.7",
        "api_base": "https://opencode.ai/zen/go/v1",
        "api_key": "os.environ/LITELLM_OPENCODE_GO_API_KEY",
    }
    assert entries["openrouter/anthropic/claude-3.7-sonnet"]["model_info"] == {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
    }
    assert entries["openrouter/hunter-alpha"]["litellm_params"] == {
        "model": "openrouter/openai/gpt-4.1-mini",
        "api_key": "os.environ/LITELLM_OPENROUTER_API_KEY",
    }
    assert entries["openrouter/hunter-alpha"]["model_info"] == {
        "input_cost_per_token": 0.0000008,
        "output_cost_per_token": 0.0000032,
    }
    assert entries["nvidia/kimi-k2.5"]["litellm_params"] == {
        "model": "nvidia/moonshotai/kimi-k2.5",
        "api_base": "https://integrate.api.nvidia.com/v1",
        "api_key": "os.environ/NVIDIA_API_KEY",
    }


def test_build_litellm_consumer_model_allowlists_project_routes_across_all_consumers() -> None:
    plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder", "my-farm-advisor", "openclaw"),
    )

    allowlists = build_litellm_consumer_model_allowlists(
        flat_env=_litellm_route_env(
            MY_FARM_ADVISOR_PRIMARY_MODEL="nvidia/kimi-k2.5",
            MY_FARM_ADVISOR_FALLBACK_MODELS="opencode-go/minimax-m2.7",
            OPENCLAW_PRIMARY_MODEL="openrouter/hunter-alpha",
            OPENCLAW_FALLBACK_MODELS="opencode-go/mimo-v2.5",
        ),
        plan=plan,
    )

    shared_models = (
        "local-model.internal/unsloth-active",
        *_EXPECTED_OPENCODE_GO_CHAT_ALIASES,
        "openrouter/anthropic/claude-3.7-sonnet",
        "openrouter/hunter-alpha",
    )

    assert allowlists == {
        "coder-hermes": shared_models,
        "coder-kdense": shared_models,
        "dokploy-ai": ("local-model.internal/unsloth-active",),
        "my-farm-advisor": (*shared_models, "nvidia/kimi-k2.5"),
        "openclaw": shared_models,
        "surfsense": shared_models,
    }
    assert "opencode-go/minimax-m2.7" in allowlists["my-farm-advisor"]


def test_build_litellm_consumer_model_allowlists_include_free_openrouter_aliases() -> None:
    plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder", "my-farm-advisor", "openclaw"),
    )

    allowlists = build_litellm_consumer_model_allowlists(
        flat_env=_litellm_route_env(
            LITELLM_OPENROUTER_MODELS=(
                "anthropic/claude-3.7-sonnet,"
                "google/gemma-4-31b-it:free,"
                "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini"
            ),
            OPENCODE_GO_BASE_URL="",
        ),
        plan=plan,
    )

    shared_models = (
        "local-model.internal/unsloth-active",
        *_EXPECTED_OPENCODE_GO_CHAT_ALIASES,
        "openrouter/anthropic/claude-3.7-sonnet",
        "openrouter/google/gemma-4-31b-it:free",
        "openrouter/hunter-alpha",
    )

    assert allowlists == {
        "coder-hermes": shared_models,
        "coder-kdense": shared_models,
        "dokploy-ai": ("local-model.internal/unsloth-active",),
        "my-farm-advisor": shared_models,
        "openclaw": shared_models,
        "surfsense": shared_models,
    }


def test_build_litellm_consumer_model_allowlists_ignore_opencode_go_wildcard_flag() -> None:
    plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder", "my-farm-advisor", "openclaw"),
    )

    allowlists = build_litellm_consumer_model_allowlists(
        flat_env=_litellm_route_env(
            LITELLM_OPENCODE_GO_WILDCARD="yes",
        ),
        plan=plan,
    )

    shared_models = (
        "local-model.internal/unsloth-active",
        *_EXPECTED_OPENCODE_GO_CHAT_ALIASES,
        "openrouter/anthropic/claude-3.7-sonnet",
        "openrouter/hunter-alpha",
    )

    assert allowlists == {
        "coder-hermes": shared_models,
        "coder-kdense": shared_models,
        "dokploy-ai": ("local-model.internal/unsloth-active",),
        "my-farm-advisor": shared_models,
        "openclaw": shared_models,
        "surfsense": shared_models,
    }
    assert "opencode-go/*" not in allowlists["openclaw"]
    assert "opencode-go/deepseek-v4-flash" in allowlists["openclaw"]
    assert "opencode-go/text-embedding-3-large" not in allowlists["openclaw"]
    assert "opencode-go/dall-e-3" not in allowlists["openclaw"]
    assert "opencode-go/whisper-1" not in allowlists["openclaw"]
    assert "opencode-go/sora-2" not in allowlists["openclaw"]
    assert "opencode-go/gpt-image-1.5" not in allowlists["openclaw"]


def test_litellm_admin_reconciliation_adopts_live_managed_keys_into_generated_state(
    tmp_path: Path,
) -> None:
    plan = build_shared_core_plan(
        stack_name="wizard-stack",
        enabled_packs=("coder", "my-farm-advisor", "openclaw"),
    )
    allowlists = build_litellm_consumer_model_allowlists(
        flat_env=_litellm_route_env(
            MY_FARM_ADVISOR_PRIMARY_MODEL="nvidia/kimi-k2.5",
            MY_FARM_ADVISOR_FALLBACK_MODELS="opencode-go/minimax-m2.7",
            OPENCLAW_PRIMARY_MODEL="openrouter/hunter-alpha",
            OPENCLAW_FALLBACK_MODELS="opencode-go/mimo-v2.5",
        ),
        plan=plan,
    )
    generated_keys = _generated_keys_for_consumers(tuple(allowlists))
    stale_models = ("local-model.internal/unsloth-active",)
    admin_api = RecordingManagedDriftLiteLLMAdminApi(
        teams={
            consumer: LiteLLMTeamRecord(
                team_id=f"team-{consumer}",
                team_alias=consumer,
                models=stale_models,
                metadata={"consumer": consumer, "managed_by": "dokploy-wizard"},
            )
            for consumer in allowlists
        },
        keys={
            consumer: LiteLLMVirtualKeyRecord(
                key=f"sk-live-{consumer}",
                key_alias=consumer,
                team_id=None,
                models=stale_models,
                metadata={"consumer": consumer, "managed_by": "dokploy-wizard"},
            )
            for consumer in allowlists
        },
    )
    backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com/api",
        api_key="dokp-test-key",
        stack_name="wizard-stack",
        plan=plan,
        litellm_generated_keys=generated_keys,
        litellm_consumer_model_allowlists=allowlists,
        litellm_admin_api=admin_api,
        client=RecordingDokploySharedCoreApi(),
        sleep_fn=lambda _: None,
        state_dir=tmp_path,
    )

    backend.reconcile_litellm_runtime()

    persisted_keys = load_litellm_generated_keys(tmp_path)
    drifted_team_consumers = {
        consumer for consumer, models in allowlists.items() if models != stale_models
    }
    assert persisted_keys is None
    assert {call["team_alias"] for call in admin_api.update_team_calls} == drifted_team_consumers
    assert {call["key_alias"] for call in admin_api.delete_key_calls} == set(allowlists)
    assert {call["key_alias"] for call in admin_api.create_key_calls} == set(allowlists)
    assert admin_api.update_key_calls == []
    for consumer, models in allowlists.items():
        if consumer in drifted_team_consumers:
            assert {
                "team_id": f"team-{consumer}",
                "team_alias": consumer,
                "models": models,
                "metadata": {"consumer": consumer, "managed_by": "dokploy-wizard"},
            } in admin_api.update_team_calls
        assert {"key_alias": consumer} in admin_api.delete_key_calls
        assert {
            "key_alias": consumer,
            "key": generated_keys.virtual_keys[consumer],
            "team_id": f"team-{consumer}",
            "models": models,
            "metadata": {"consumer": consumer, "managed_by": "dokploy-wizard"},
        } in admin_api.create_key_calls


def test_no_ai_pack_install_includes_litellm(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "core-only.env"
    raw_env = _core_only_raw_env()
    api = RecordingDokploySharedCoreApi()
    networking_backend = FakeCloudflareBackend()

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=_litellm_shared_core_backend(raw_env, api, state_dir=state_dir),
    )

    compose = api.compose_files_by_name["wizard-stack-shared"]
    loaded_state = load_state_dir(state_dir)
    generated_keys = load_litellm_generated_keys(state_dir)

    assert summary["desired_state"]["enabled_packs"] == []
    assert summary["lifecycle"]["mode"] == "install"
    assert summary["lifecycle"]["phases_to_run"] == [
        "dokploy_bootstrap",
        "networking",
        "shared_core",
    ]
    assert summary["shared_core"]["outcome"] == "applied"
    assert summary["shared_core"]["litellm"]["resource_name"] == "wizard-stack-shared-litellm"
    assert "  wizard-stack-shared-litellm:\n" in compose
    assert "image: ghcr.io/berriai/litellm:" in compose
    assert 'DATABASE_URL: "postgresql://wizard_stack_litellm:' in compose
    assert "LITELLM_VIRTUAL_KEY_OPENCLAW" not in compose
    assert "LITELLM_VIRTUAL_KEY_MY_FARM_ADVISOR" not in compose
    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
    )
    assert loaded_state.ownership_ledger is not None
    assert {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
    } == {
        ("cloudflare_tunnel", "account:account-123"),
        ("cloudflare_dns_record", "zone:zone-123:dokploy.example.com"),
        ("shared_core_litellm", "stack:wizard-stack:shared-litellm"),
        ("shared_core_network", "stack:wizard-stack:shared-network"),
        ("shared_core_postgres", "stack:wizard-stack:shared-postgres"),
    }
    assert generated_keys is not None
    assert set(generated_keys.virtual_keys) == {
        "coder-hermes",
        "coder-kdense",
        "dokploy-ai",
        "my-farm-advisor",
        "openclaw",
        "surfsense",
    }


def test_core_only_rerun_is_noop_and_preserves_litellm_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "core-only-rerun.env"
    raw_env = _core_only_raw_env()
    api = RecordingDokploySharedCoreApi()
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = _litellm_shared_core_backend(raw_env, api, state_dir=state_dir)

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
    )
    generated_before = load_litellm_generated_keys(state_dir)

    monkeypatch.setattr(dokploy_wizard.cli, "validate_preserved_phases", lambda **_: None)

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
    )
    generated_after = load_litellm_generated_keys(state_dir)

    assert summary["lifecycle"]["mode"] == "noop"
    assert summary["lifecycle"]["phases_to_run"] == []
    assert summary["shared_core"]["outcome"] == "already_present"
    assert summary["shared_core"]["litellm"]["action"] == "reuse_owned"
    assert generated_after == generated_before
    assert api.create_project_calls == 1
    assert api.create_compose_calls == 1
    assert api.update_compose_calls == 1
    assert api.deploy_calls == 1


def test_modify_litellm_alias_change_keeps_generated_keys_stable_for_coder_and_farm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "coder-farm.env"
    shared_core_api = RecordingDokploySharedCoreApi()
    networking_backend = NetworkingCloudflareBackend()
    coder_backend = FakeCoderBackend()
    openclaw_backend = FakeOpenClawBackend()
    initial_raw = RawEnvInput(
        format_version=1,
        values=_base_install_values(
            ENABLE_CODER="true",
            ENABLE_MY_FARM_ADVISOR="true",
            MY_FARM_ADVISOR_CHANNELS="telegram",
            AI_DEFAULT_API_KEY="shared-ai-key",
            AI_DEFAULT_BASE_URL="https://models.example.com/v1",
            MY_FARM_ADVISOR_PRIMARY_MODEL="openrouter/hunter-alpha",
            OPENCODE_GO_API_KEY="opencode-go-key",
            LITELLM_OPENROUTER_MODELS="openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini",
            CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
        ),
    )
    modified_raw = RawEnvInput(
        format_version=1,
        values=_base_install_values(
            ENABLE_CODER="true",
            ENABLE_MY_FARM_ADVISOR="true",
            MY_FARM_ADVISOR_CHANNELS="telegram",
            AI_DEFAULT_API_KEY="shared-ai-key",
            AI_DEFAULT_BASE_URL="https://models.example.com/v1",
            MY_FARM_ADVISOR_PRIMARY_MODEL="openrouter/hunter-alpha",
            MY_FARM_ADVISOR_FALLBACK_MODELS="openrouter/healer-alpha",
            OPENCODE_GO_API_KEY="opencode-go-key",
            LITELLM_OPENROUTER_MODELS=(
                "openrouter/hunter-alpha=openrouter/openai/gpt-4.1-mini,"
                "openrouter/healer-alpha=openrouter/anthropic/claude-3.7-sonnet"
            ),
            CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
        ),
    )

    monkeypatch.setattr(dokploy_wizard.cli, "_can_reuse_existing_dokploy_api_key", lambda **_: True)
    monkeypatch.setattr(dokploy_wizard.cli, "_qualify_dokploy_mutation_auth", lambda **_: None)

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=initial_raw,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=_litellm_shared_core_backend(
            initial_raw,
            shared_core_api,
            state_dir=state_dir,
        ),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        coder_backend=coder_backend,
        openclaw_backend=openclaw_backend,
    )
    generated_before = load_litellm_generated_keys(state_dir)

    summary = run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=modified_raw,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=_litellm_shared_core_backend(
            modified_raw,
            shared_core_api,
            state_dir=state_dir,
        ),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        coder_backend=coder_backend,
        openclaw_backend=openclaw_backend,
    )
    generated_after = load_litellm_generated_keys(state_dir)

    assert summary["lifecycle"]["mode"] == "modify"
    assert summary["lifecycle"]["phases_to_run"] == [
        "shared_core",
        "coder",
        "my-farm-advisor",
    ]
    assert summary["shared_core"]["outcome"] == "already_present"
    assert summary["coder"]["outcome"] in {"applied", "already_present"}
    assert summary["my_farm_advisor"]["outcome"] in {"applied", "already_present"}
    assert shared_core_api.update_compose_calls == 2
    assert openclaw_backend.update_calls == 1
    assert "openrouter/healer-alpha" in shared_core_api.compose_files_by_name["wizard-stack-shared"]
    assert generated_before is not None
    assert generated_after == generated_before
    assert generated_after is not None
    assert len(set(generated_after.virtual_keys.values())) == len(generated_after.virtual_keys)


def test_coder_and_advisor_install_persist_consumer_specific_virtual_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "coder-openclaw-farm.env"
    api = RecordingDokployOpenClawApi()

    _patch_real_dokploy_openclaw_backend(monkeypatch, api)
    monkeypatch.setattr(dokploy_wizard.cli, "_can_reuse_existing_dokploy_api_key", lambda **_: True)
    monkeypatch.setattr(dokploy_wizard.cli, "_qualify_dokploy_mutation_auth", lambda **_: None)

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=RawEnvInput(
            format_version=1,
            values=_base_install_values(
                ENABLE_CODER="true",
                ENABLE_OPENCLAW="true",
                OPENCLAW_CHANNELS="telegram",
                ENABLE_MY_FARM_ADVISOR="true",
                MY_FARM_ADVISOR_CHANNELS="telegram",
                AI_DEFAULT_API_KEY="shared-ai-key",
                AI_DEFAULT_BASE_URL="https://models.example.com/v1",
                MY_FARM_ADVISOR_PRIMARY_MODEL="anthropic/claude-sonnet-4",
                CLOUDFLARE_ACCESS_OTP_EMAILS="admin@example.com",
            ),
        ),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=NetworkingCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        coder_backend=FakeCoderBackend(),
        openclaw_backend=None,
    )

    generated_keys = load_litellm_generated_keys(state_dir)
    assert generated_keys is not None
    assert set(generated_keys.virtual_keys) == {
        "coder-hermes",
        "coder-kdense",
        "dokploy-ai",
        "my-farm-advisor",
        "openclaw",
        "surfsense",
    }
    assert len(set(generated_keys.virtual_keys.values())) == 6
    assert api.compose_files_by_name["wizard-stack-openclaw"]
    assert api.compose_files_by_name["wizard-stack-my-farm-advisor"]
