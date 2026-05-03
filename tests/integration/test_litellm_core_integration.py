# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

import dokploy_wizard.cli
from dokploy_wizard.cli import run_install_flow, run_modify_flow
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
from dokploy_wizard.state import (
    RawEnvInput,
    load_litellm_generated_keys,
    load_state_dir,
    resolve_desired_state,
)
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

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        self.update_compose_calls += 1
        name = self.compose_names_by_id[compose_id]
        self.compose_files_by_name[name] = compose_file
        return DokployComposeRecord(compose_id=compose_id, name=name)

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message=None)


def _core_only_raw_env(**overrides: str) -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values=_base_install_values(PACKS="", **overrides),
    )


def _litellm_shared_core_backend(
    raw_env: RawEnvInput,
    client: RecordingDokploySharedCoreApi,
) -> DokploySharedCoreBackend:
    desired_state = resolve_desired_state(raw_env)
    return DokploySharedCoreBackend(
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
    )


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
        shared_core_backend=_litellm_shared_core_backend(raw_env, api),
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
        "my-farm-advisor",
        "openclaw",
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
    shared_core_backend = _litellm_shared_core_backend(raw_env, api)

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
    assert api.update_compose_calls == 0
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
        shared_core_backend=_litellm_shared_core_backend(initial_raw, shared_core_api),
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
        shared_core_backend=_litellm_shared_core_backend(modified_raw, shared_core_api),
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
    assert shared_core_api.update_compose_calls == 1
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
        "my-farm-advisor",
        "openclaw",
    }
    assert len(set(generated_keys.virtual_keys.values())) == 4
    assert api.compose_files_by_name["wizard-stack-openclaw"]
    assert api.compose_files_by_name["wizard-stack-my-farm-advisor"]
