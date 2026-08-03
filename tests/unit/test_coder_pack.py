# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
import subprocess
from base64 import b64decode
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest

import dokploy_wizard.dokploy.coder as coder_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.coder import DokployCoderApi, DokployCoderBackend, _render_compose_file
from dokploy_wizard.dokploy.coder_migration_types import CoderId
from dokploy_wizard.dokploy.coder_secret_client import DockerExecCoderSecretClient
from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptStep,
    CoderSecretReceiptStore,
    metadata_sha256,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation import (
    CoderSecretError,
    CoderSecretMetadata,
    CoderSecretReconciler,
    CoderSecretSpec,
)
from dokploy_wizard.dokploy.coder_template_migration_runtime import (
    ProductionMigrationInputs,
    TemplateMigrationExecutionError,
)
from dokploy_wizard.packs.coder import build_coder_ledger, reconcile_coder
from dokploy_wizard.packs.coder.models import CoderResourceRecord
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    resolve_desired_state,
    write_applied_checkpoint,
)

from .fake_dokploy import FakeDokployApiClient
from .test_coder_secret_reconciliation import FakeCoderSecrets
from .test_coder_template_migration import (
    _ORGANIZATION_ID as MIGRATION_ORGANIZATION_ID,
)
from .test_coder_template_migration import (
    _PRIMARY_ID as MIGRATION_PRIMARY_ID,
)
from .test_coder_template_migration import (
    _WORKSPACE_ID as MIGRATION_WORKSPACE_ID,
)
from .test_coder_template_migration import (
    _Api as MigrationApiFake,
)
from .test_coder_template_migration import (
    _build as migration_build,
)
from .test_coder_template_migration import (
    _CrashOnce as MigrationCrashOnce,
)
from .test_coder_template_migration import (
    _migration as migration_runner,
)
from .test_coder_template_migration import (
    _Pusher as MigrationPusherFake,
)
from .test_coder_template_migration import (
    _targets as migration_targets,
)


def test_secret_value_hash_workspace_collects_only_agent_hash_and_deletes_by_id(
    tmp_path: Path,
) -> None:
    value = "SECRET-CODER-HERMES"
    expected_hash = sha256(value.encode()).hexdigest()
    outputs = iter(
        (
            '[{"id":"00000000-0000-4000-8000-000000000003","name":"ubuntu-vscode-opencode-pi"}]',
            "",
            '[{"id":"00000000-0000-4000-8000-000000000001","name":"proof-workspace","owner_id":"00000000-0000-4000-8000-000000000002","owner_name":"admin","template_id":"00000000-0000-4000-8000-000000000003","template_name":"ubuntu-vscode-opencode-pi","latest_build":{"status":"running"}}]',
            '[{"id":"00000000-0000-4000-8000-000000000001","name":"proof-workspace","owner_id":"00000000-0000-4000-8000-000000000002","owner_name":"admin","template_id":"00000000-0000-4000-8000-000000000003","template_name":"ubuntu-vscode-opencode-pi","latest_build":{"status":"running"}}]',
            '[{"id":"00000000-0000-4000-8000-000000000001","name":"proof-workspace","owner_id":"00000000-0000-4000-8000-000000000002","owner_name":"admin","template_id":"00000000-0000-4000-8000-000000000003","template_name":"ubuntu-vscode-opencode-pi","latest_build":{"status":"running"}}]',
            f"{expected_hash}\n",
            '[{"id":"00000000-0000-4000-8000-000000000001","name":"proof-workspace","owner_id":"00000000-0000-4000-8000-000000000002","owner_name":"admin","template_id":"00000000-0000-4000-8000-000000000003","template_name":"ubuntu-vscode-opencode-pi","latest_build":{"status":"running"}}]',
            "",
            "[]",
        )
    )
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def runner(
        arguments: tuple[str, ...],
        *,
        input: str | None,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, env
        assert timeout == 60
        calls.append((arguments, input))
        return subprocess.CompletedProcess((), 0, stdout=next(outputs), stderr="")

    client = DockerExecCoderSecretClient(
        container_name="coder-container",
        session_token="session-token",
        state_dir=tmp_path,
        runner=runner,
        workspace_name="proof-workspace",
    )
    spec = CoderSecretSpec(
        name="hermes-openai-api-key",
        env_name="OPENAI_API_KEY",
        value=value,
        description="Hermes LiteLLM key",
    )

    observed_hash = client.verify_workspace_value_hash(spec, "a" * 64)

    assert observed_hash == expected_hash
    assert any(call[-3:] == ("delete", "--yes", "00000000-0000-4000-8000-000000000001") for call, _ in calls)
    assert all(value not in argument for call, _ in calls for argument in call)


def _expected_coder_fallback_models_json() -> str:
    return coder_module._litellm_workspace_fallback_models_json(
        default_alias="opencode-go/deepseek-v4-flash"
    )


def _expected_coder_fallback_models_json_escaped() -> str:
    return coder_module._shell_double_quote_escape(_expected_coder_fallback_models_json())


_FIXTURE_RUNTIME_IMAGE_REPLACEMENTS = {
    "__DOKPLOY_WIZARD_RUNTIME_IMAGE_AMD64__": "sha256:" + "a" * 64,
    "__DOKPLOY_WIZARD_RUNTIME_IMAGE_ARM64__": "sha256:" + "b" * 64,
}


def _patch_workspace_runtime_image_replacements(
    monkeypatch: pytest.MonkeyPatch, backend: DokployCoderBackend
) -> None:
    monkeypatch.setattr(
        backend,
        "_workspace_runtime_image_replacements",
        lambda: dict(_FIXTURE_RUNTIME_IMAGE_REPLACEMENTS),
    )


def _without_runtime_image_replacements(replacements: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in replacements.items()
        if name not in _FIXTURE_RUNTIME_IMAGE_REPLACEMENTS
    }


def _patch_template_migration(
    monkeypatch: pytest.MonkeyPatch,
    replacements_by_name: dict[str, dict[str, str] | None] | None = None,
) -> list[ProductionMigrationInputs]:
    calls: list[ProductionMigrationInputs] = []

    def capture(inputs: ProductionMigrationInputs) -> None:
        calls.append(inputs)
        if replacements_by_name is not None:
            replacements_by_name.update(
                {
                    source.name: _without_runtime_image_replacements(dict(source.replacements))
                    for source in inputs.sources
                }
            )

    monkeypatch.setattr(coder_module, "execute_template_migration", capture)
    return calls


def _task1_coder_secret_lock_payload():
    return json.loads(
        files("dokploy_wizard.dokploy")
        .joinpath("task1_coder_secret_lock.json")
        .read_text(encoding="utf-8")
    )


def _task1_hermes_record() -> dict[str, str]:
    payload = _task1_coder_secret_lock_payload()
    return next(
        record
        for record in payload["records"]
        if record["secret_name"] == "hermes-inference-provider"
    )


def test_coder_litellm_fallback_models_json_uses_full_concrete_aliases() -> None:
    aliases = json.loads(_expected_coder_fallback_models_json())

    assert aliases[0] == "opencode-go/deepseek-v4-flash"
    assert "opencode-go/deepseek-v4-flash" in aliases
    assert "opencode-go/minimax-m2.7" in aliases
    assert "openrouter/minimax/minimax-m2.5:free" in aliases
    assert "deepseek-v4-flash" not in aliases
    assert "minimax/minimax-m2.5:free" not in aliases
    assert "opencode-go/*" not in aliases


def test_copilot_byok_contract_documents_official_chat_only_scope() -> None:
    contract = coder_module._copilot_byok_contract()

    assert contract["setting"] == "github.copilot.chat.customOAIModels"
    assert contract["provider_label"] == "Dokploy LiteLLM"
    assert contract["scope"] == "chat-and-agents-only"
    assert "inline completions" in contract["limitation"]
    assert "code.visualstudio.com/docs/copilot/customization/language-models" in contract["reference"]


def test_copilot_byok_settings_json_contains_litellm_models() -> None:
    settings = json.loads(
        coder_module._copilot_byok_settings_json(
            base_url="http://wizard-stack-shared-litellm:4000",
            api_key="fake-litellm-key",
            default_alias="local-model.internal/unsloth-active",
            fallback_models_json=_expected_coder_fallback_models_json(),
        )
    )

    custom_models = settings["github.copilot.chat.customOAIModels"]
    assert "local-model.internal/unsloth-active" in custom_models
    assert "opencode-go/deepseek-v4-flash" in custom_models
    default_model = custom_models["local-model.internal/unsloth-active"]
    assert default_model["name"] == "Dokploy LiteLLM: local-model.internal/unsloth-active"
    assert default_model["url"] == "http://wizard-stack-shared-litellm:4000/v1"
    assert default_model["apiKey"] == "fake-litellm-key"
    assert default_model["requiresAPIKey"] is True
    assert default_model["toolCalling"] is True
    assert default_model["vision"] is False
    assert default_model["thinking"] is False
    assert "sk-" not in json.dumps(settings)
    assert "ghp_" not in json.dumps(settings)


def test_copilot_byok_compatibility_probe_is_secret_safe() -> None:
    probe = coder_module._copilot_byok_compatibility_probe()

    assert "official-byok-supported" in probe
    assert "github.copilot.chat.customOAIModels" in probe
    assert "chat-and-agents-only" in probe
    assert "secret" in probe.lower()
    assert "sk-" not in probe
    assert "ghp_" not in probe


def test_coder_live_template_update_command_bundle_is_non_destructive_and_redacted() -> None:
    bundle = "\n".join(coder_module._coder_live_template_update_command_bundle())

    assert "inspect-state" in bundle
    assert "focused-coder-template-reseed" in bundle
    assert "ensure_application_ready" in bundle
    assert "github.copilot.chat.customOAIModels" in bundle
    assert "<redacted" in bundle
    assert "uninstall" not in bundle
    assert "destroy-data" not in bundle
    assert "reinstall" not in bundle.lower()
    assert "sk-" not in bundle
    assert "ghp_" not in bundle


def test_coder_state_gap_diagnosis_requires_focused_reseed_when_checkpoint_incomplete() -> None:
    diagnosis = coder_module._coder_state_gap_diagnosis(("desired-state.json", "raw-input.json"))

    assert "state-incomplete" in diagnosis
    assert "focused-coder-template-reseed-required" in diagnosis
    assert "applied-state.json" in diagnosis
    assert "ownership-ledger.json" in diagnosis
    assert "do-not-reinstall" in diagnosis


def _assert_template_preseeds_copilot_byok(template: str) -> None:
    assert "github.copilot.chat.customOAIModels" in template
    assert "Official Copilot BYOK is intentionally chat/agent-only" in template
    assert "/home/coder/.local/share/code-server/User/settings.json" in template
    assert "/home/coder/.config/code-server/User/settings.json" in template
    assert "Dokploy LiteLLM: {model_id}" in template
    assert '"toolCalling": True' in template
    assert '"vision": False' in template
    assert '"thinking": False' in template
    assert "vivswan.litellm-vscode-chat" not in template
    assert "calgan.oai-provider" not in template
    assert "openai-compat-provider.providers" not in template


def test_base_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'resource "coder_script" "model_sync_start"' in template
    assert "workspace-catalog-sync.pyz --adapter primary" in template
    assert "github.copilot.chat.customOAIModels" not in template


def test_pi_web_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-pi-web/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert 'Path("/home/coder/.pi/agent/models.json").write_text(' in template


def test_opencode_web_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-opencode-web/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'resource "coder_script" "model_sync_start"' in template
    assert "workspace-catalog-sync.pyz --adapter opencode-web" in template
    assert "github.copilot.chat.customOAIModels" not in template
    assert "OPENCODE_WEB_PORT=4096" in template
    assert "OPENCODE_PROXY_PORT=4097" in template


def test_shared_package_digest_changes_when_model_sync_utility_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    template_dir = coder_module._default_template_dir()
    baseline = coder_module._template_version_name(template_dir=template_dir, replacements=None)
    replacement = tmp_path / "workspace_catalog_sync_runtime.py"
    sources = coder_module._workspace_model_sync_utility_sources()
    runtime_source = next(
        source
        for source, archive_path in sources
        if archive_path == "dokploy_wizard/dokploy/workspace_catalog_sync_runtime.py"
    )
    replacement.write_bytes(runtime_source.read_bytes() + b"\n# changed for digest coverage\n")

    def changed_sources() -> tuple[tuple[Path, str], ...]:
        return tuple(
            (replacement if source == runtime_source else source, archive_path)
            for source, archive_path in sources
        )

    monkeypatch.setattr(coder_module, "_workspace_model_sync_utility_sources", changed_sources)

    # When
    changed = coder_module._template_version_name(template_dir=template_dir, replacements=None)

    # Then
    assert changed != baseline
    with coder_module._rendered_template_dir(template_dir=template_dir, replacements=None) as rendered:
        assert (rendered / ".dokploy-wizard/model-sync/workspace-catalog-sync.pyz").is_file()


def test_shared_package_zipapp_is_executable_without_application_dependencies() -> None:
    # Given
    with coder_module._rendered_template_dir(
        template_dir=coder_module._default_template_dir(), replacements=None
    ) as rendered:
        utility = rendered / ".dokploy-wizard/model-sync/workspace-catalog-sync.pyz"

        # When
        result = subprocess.run(
            ["python3", str(utility), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    # Then
    assert result.returncode == 0
    assert "--adapter" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("template_path", "adapter"),
    (
        ("templates/coder/default-ubuntu-code-server/main.tf", "primary"),
        ("templates/coder/default-ubuntu-code-server-opencode-web/main.tf", "opencode-web"),
    ),
)
def test_model_sync_scripts_use_exact_start_and_periodic_coder_contract(
    template_path: str, adapter: str
) -> None:
    # Given
    template = Path(template_path).read_text(encoding="utf-8")

    # When / Then
    assert 'resource "coder_script" "model_sync_start"' in template
    assert "run_on_start       = true" in template
    assert "start_blocks_login = false" in template
    assert 'resource "coder_script" "model_sync_periodic"' in template
    assert 'cron         = "0 */15 * * * *"' in template
    assert (
        f"workspace-catalog-sync.pyz --adapter {adapter} --workspace-root /home/coder"
        in template
    )
    assert 'content_base64 = filebase64("${path.module}' in template
    assert "pkill -f" not in template
    assert "systemctl restart" not in template


def test_openwork_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-openwork/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert "OPENWORK_UI_PORT=8790" in template
    assert "OPENWORK_PROXY_PORT=8788" in template


def test_hermes_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-hermes/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert 'export HERMES_TEMPLATE_API_KEY="$${LITELLM_VIRTUAL_KEY_CODER_HERMES}"' in template
    assert 'upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"' in template


def _legacy_kdense_copilot_byok_uses_central_gateway() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert 'export KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL="__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__"' in template
    assert 'export KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY="__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__"' in template
    assert 'export KDENSE_COPILOT_DEFAULT_ALIAS="__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__/__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__"' in template
    assert 'base_url = os.environ["KDENSE_CENTRAL_LITELLM_BASE_URL"].rstrip("/")' in template
    assert 'api_key = os.environ.get("KDENSE_CENTRAL_LITELLM_API_KEY", "")' in template
    assert 'KDENSE_LOCAL_LITELLM_BASE_URL="http://localhost:$KDENSE_LITELLM_PORT"' in template
    assert 'append_env OPENROUTER_API_KEY ' not in template
    assert 'append_env NVIDIA_API_KEY ' not in template
    assert 'append_env ANTHROPIC_API_KEY ' not in template


def test_kdense_secret_leak_template_uses_only_the_scoped_virtual_key() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'KDENSE_LITELLM_BASE_URL="__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__"' in template
    assert 'KDENSE_LITELLM_API_KEY="__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__"' in template
    assert "KDENSE_OPENCODE_GO" not in template
    assert "OPENROUTER_API_KEY" not in template
    assert "ANTHROPIC_API_KEY" not in template
    assert "NVIDIA_API_KEY" not in template
    assert "EXA_API_KEY" not in template
    assert "MODAL_TOKEN" not in template


@dataclass
class FakeCoderBackend:
    existing_service: CoderResourceRecord | None = None
    existing_data: CoderResourceRecord | None = None
    health_ok: bool = True
    health_results: list[bool] | None = None
    ensure_calls: int = 0

    def get_service(self, resource_id: str) -> CoderResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: object) -> CoderResourceRecord:
        resource_name = str(kwargs["resource_name"])
        self.existing_service = CoderResourceRecord(
            resource_id="coder-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def update_service(self, **kwargs: object) -> CoderResourceRecord:
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> CoderResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> CoderResourceRecord:
        self.existing_data = CoderResourceRecord(
            resource_id="coder-data-1", resource_name=resource_name
        )
        return self.existing_data

    def check_health(self, *, service: CoderResourceRecord, url: str) -> bool:
        del service, url
        if self.health_results is not None:
            if self.health_results:
                return self.health_results.pop(0)
            return self.health_ok
        return self.health_ok

    def ensure_application_ready(self) -> tuple[str, ...]:
        self.ensure_calls += 1
        return ()


@dataclass
class FakeCoderApi:
    last_create_compose_file: str | None = None

    def list_projects(self):
        return ()

    def create_project(self, *, name: str, description: str | None, env: str | None):
        class Created:
            project_id = "project-1"
            environment_id = "env-1"

        return Created()

    def create_compose(self, *, name: str, environment_id: str, compose_file: str, app_name: str):
        del name, environment_id, app_name
        self.last_create_compose_file = compose_file

        class Compose:
            compose_id = "compose-1"

        return Compose()

    def update_compose(self, *, compose_id: str, compose_file: str | None = None, env: str | None = None):
        del compose_id, env
        if compose_file is not None:
            self.last_create_compose_file = compose_file

        class Compose:
            compose_id = "compose-1"

        return Compose()

    def deploy_compose(self, *, compose_id: str, title: str | None, description: str | None):
        del compose_id, title, description

        class Deploy:
            success = True
            message = "ok"

        return Deploy()


def test_render_coder_compose_includes_root_and_wildcard_routes() -> None:
    rendered = _render_compose_file(
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
    )
    compose = rendered.compose_file

    assert 'CODER_ACCESS_URL: "https://coder.example.com/"' in compose
    assert 'CODER_WILDCARD_ACCESS_URL: "*.coder.example.com"' in compose
    assert (
        'CODER_PG_CONNECTION_URL: "${CODER_PG_CONNECTION_URL:?CODER_PG_CONNECTION_URL is required}"'
        in compose
    )
    assert rendered.env_specs[0].value == "postgres://wizard_stack_coder:change-me@wizard-stack-shared-postgres:5432/wizard_stack_coder?sslmode=disable"
    assert 'CODER_PROXY_TRUSTED_HEADERS: "X-Forwarded-For"' in compose
    assert 'CODER_PROXY_TRUSTED_ORIGINS: "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"' in compose
    assert "CODER_REDIRECT_TO_ACCESS_URL:" not in compose
    assert '    user: "0:0"' in compose
    assert "      - /var/run/docker.sock:/var/run/docker.sock" in compose
    assert 'traefik.http.routers.wizard-stack-coder.rule: "Host(`coder.example.com`)"' in compose
    assert (
        'traefik.http.routers.wizard-stack-coder.middlewares: "wizard-stack-coder-forwarded-https,wizard-stack-coder-forwarded-host"'
        in compose
    )
    assert (
        'traefik.http.routers.wizard-stack-coder-wildcard.rule: "HostRegexp(`(?i)^[a-z0-9-]+(?:--[a-z0-9-]+){2,}\\\\.coder\\\\.example\\\\.com$`)"'
        in compose
    )
    assert (
        'traefik.http.routers.wizard-stack-coder-wildcard.middlewares: "wizard-stack-coder-forwarded-https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-coder-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-coder-forwarded-host.headers.customrequestheaders.X-Forwarded-Host: "coder.example.com"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-coder-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"'
        in compose
    )
    assert 'traefik.http.services.wizard-stack-coder.loadbalancer.server.port: "3000"' in compose
    assert "traefik.hz" not in compose
    assert compose.count("traefik.http.routers.wizard-stack-coder.rule:") == 1
    assert compose.count("traefik.http.routers.wizard-stack-coder-wildcard.rule:") == 1
    assert compose.count("traefik.http.routers.wizard-stack-coder-wildcard.middlewares:") == 1
    assert compose.count("traefik.http.routers.wizard-stack-coder-wildcard.tls:") == 1
    assert (
        compose.count(
            "traefik.http.middlewares.wizard-stack-coder-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: \"https\""
        )
        == 1
    )
    assert (
        compose.count(
            "traefik.http.middlewares.wizard-stack-coder-forwarded-host.headers.customrequestheaders.X-Forwarded-Host: \"coder.example.com\""
        )
        == 1
    )


def test_default_coder_template_requires_prebuilt_runtime_tools() -> None:
    template = Path("templates/coder/default-ubuntu-code-server/main.tf").read_text(
        encoding="utf-8"
    )

    assert "for runtime_command in curl git wget btop python3 opencode zellij node pi; do" in template
    assert "apt-get install" not in template
    assert "nodesource.com" not in template
    assert "opencode.ai/install" not in template
    assert "pnpm add -g" not in template
    assert "DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS" in template
    assert 'export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"' in template
    assert "workspace-catalog-sync.pyz --adapter primary" in template
    assert 'content_base64 = filebase64("${path.module}' in template
    assert "urllib.request" not in template
    assert "github.copilot.chat.customOAIModels" not in template
    assert "pi.dev/install.sh" not in template
    assert 'resource "coder_app"' not in template
    assert "pi-web-ui" not in template
    assert "vite preview" not in template
    assert "subdomain =" not in template


def test_default_opencode_web_template_includes_web_app() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-opencode-web/main.tf").read_text(
        encoding="utf-8"
    )

    assert "for runtime_command in opencode zellij node; do" in template
    assert "apt-get install" not in template
    assert "opencode.ai/install" not in template
    assert "DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS" in template
    assert "workspace-catalog-sync.pyz --adapter opencode-web" in template
    assert 'export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"' in template
    assert 'content_base64 = filebase64("${path.module}' in template
    assert "urllib.request" not in template
    assert "github.copilot.chat.customOAIModels" not in template
    assert "OPENCODE_WEB_PORT=4096" in template
    assert "OPENCODE_PROXY_PORT=4097" in template
    assert (
        'nohup opencode web --hostname 127.0.0.1 --port "$OPENCODE_WEB_PORT" >/tmp/opencode-web.log 2>&1 &'
        in template
    )
    assert "cat >/tmp/coder-mounted-proxy.mjs <<'JS'" in template
    assert '"accept-encoding"' in template
    assert "content-security-policy" in template
    assert "content-encoding" in template
    assert "pageHttpOrigin + mount + next.pathname + next.search" in template
    assert "const requestInitFrom = async (request, init) => {" in template
    assert "requestInit.body = await request.clone().arrayBuffer();" in template
    assert (
        "if (input instanceof Request) return originalFetch(url, await requestInitFrom(input, init));"
        in template
    )
    assert "window.EventSource = class extends OriginalEventSource" in template
    assert "window.WebSocket = class extends OriginalWebSocket" in template
    assert "originalPushState = window.history.pushState" in template
    assert "window.__OPENCODE_MOUNT = mount;" in template
    assert "const mountedBaseScript" in template
    assert "document.head.prepend(base);" in template
    assert "coder-mount=v2" in template
    assert 'responseHeaders["Cache-Control"] = "no-store";' in template
    assert '.replace("<head>", "<head>" + mountedBaseScript)' in template
    assert 'KO=function(e){let t="";const n=location.pathname.indexOf("/apps/");' in template
    assert 'import("./$1?coder-mount=v2")' in template
    assert 'from"./$1?coder-mount=v2"' in template
    assert (
        'window.history.replaceState(window.history.state, "", "/L2hvbWUvY29kZXI/session");'
        in template
    )
    assert 'path:"/:coderUser/:coderWorkspace/apps/:coderApp/:dir"' in template
    assert (
        'nohup env TARGET_PORT="$OPENCODE_WEB_PORT" PROXY_PORT="$OPENCODE_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs'
        in template
    )
    assert 'resource "coder_app" "opencode"' in template
    assert 'display_name = "OpenCode"' in template
    assert 'icon         = "/opt/dokploy-wizard/runtime/icons/opencode.svg"' not in template
    assert 'url          = "http://localhost:4097"' in template
    assert 'share        = "owner"' in template
    assert "subdomain    = false" in template
    assert 'url       = "http://localhost:4097"' in template


def test_default_openwork_template_includes_full_webui_stack() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-openwork/main.tf").read_text(
        encoding="utf-8"
    )

    assert "for runtime_command in curl git wget btop python3 opencode zellij node; do" in template
    assert "apt-get install" not in template
    assert "corepack enable" in template
    assert "npm install -g openwork-orchestrator" in template
    assert "CI=true pnpm install" in template
    assert (
        "Shared LiteLLM defaults keep OpenWork's embedded OpenCode routes aligned with the wizard-managed gateway."
        in template
    )
    assert 'export AI_DEFAULT_PROVIDER="$${AI_DEFAULT_PROVIDER:-__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__}"' in template
    assert 'export AI_DEFAULT_MODEL="$${AI_DEFAULT_MODEL:-__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__}"' in template
    assert 'export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__}"' in template
    assert 'export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__}"' in template
    assert 'export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$AI_DEFAULT_BASE_URL}"' in template
    assert 'export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$AI_DEFAULT_API_KEY}"' in template
    assert 'export LITELLM_DEFAULT_ALIAS="$AI_DEFAULT_PROVIDER/$AI_DEFAULT_MODEL"' in template
    assert 'export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"' in template
    assert 'with urllib.request.urlopen(request, timeout=5) as response:' in template
    assert 'payload = json.load(response)' in template
    assert 'payload = {"data": []}' in template
    assert 'model_ids = list(dict.fromkeys(model_ids + fallback_models))' in template
    assert '"npm": "@ai-sdk/openai-compatible"' in template
    assert '"options": {"baseURL": base_url, "apiKey": api_key}' in template
    assert '"models": {model_id: {} for model_id in model_ids}' in template
    assert 'Path("/home/coder/.config/opencode/opencode.json").write_text(' in template
    assert 'OPENWORK_SRC_DIR=/home/coder/.cache/openwork-src' in template
    assert 'git clone --depth 1 --branch dev https://github.com/different-ai/openwork' in template
    assert (
        'OPENWORK_APPROVAL_MODE=auto OPENWORK_PORT=$OPENWORK_SERVER_PORT OPENWORK_TOKEN="$OPENWORK_CLIENT_TOKEN" OPENWORK_HOST_TOKEN="$OPENWORK_HOST_TOKEN" nohup openwork serve --workspace /home/coder --json'
        in template
    )
    assert (
        'nohup sh -lc "cd \'$OPENWORK_SRC_DIR/apps/app\' && pnpm exec vite preview --host 127.0.0.1 --port $OPENWORK_UI_PORT --strictPort"'
        in template
    )
    assert 'localStorage.setItem("openwork.server.urlOverride", baseUrl);' in template
    assert 'localStorage.setItem("openwork.server.token"' in template
    assert 'localStorage.setItem("openwork.server.active", baseUrl' in template
    assert "function isStaticAsset(pathname)" in template
    assert 'raw === mount || raw.startsWith(mount + "/")' in template
    assert "await input.clone().arrayBuffer()" in template
    assert "originalFetch(new Request(url, next))" in template
    assert "cat >/tmp/coder-mounted-proxy.mjs <<'JS'" in template
    assert (
        'nohup env UI_PORT="$OPENWORK_UI_PORT" API_PORT="$OPENWORK_SERVER_PORT" PROXY_PORT="$OPENWORK_PROXY_PORT" CLIENT_TOKEN="$OPENWORK_OWNER_TOKEN" node /tmp/coder-mounted-proxy.mjs'
        in template
    )
    assert 'resource "coder_app" "openwork"' in template
    assert 'slug         = "openwork"' in template
    assert 'display_name = "OpenWork"' in template
    assert 'icon         = "/opt/dokploy-wizard/runtime/icons/openwork.svg"' not in template
    assert 'url          = "http://localhost:8788"' in template
    assert "subdomain    = false" in template
    assert 'url       = "http://localhost:8788/health"' in template


def test_required_template_names_are_the_exact_four_retained_templates() -> None:
    assert coder_module._default_pi_web_template_dir() == (
        Path(coder_module.__file__).resolve().parents[3]
        / "templates"
        / "coder"
        / "default-ubuntu-code-server-pi-web"
    )
    assert coder_module._default_pi_web_template_name() == "ubuntu-vscode-pi-web"
    assert coder_module._required_template_names() == (
        coder_module._default_template_name(),
        coder_module._default_opencode_web_template_name(),
        coder_module._default_hermes_template_name(),
        coder_module._default_kdense_byok_template_name(),
    )
    assert len(coder_module._required_template_names()) == 4


def test_default_pi_web_template_includes_clickable_pi_web_ui() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-pi-web/main.tf").read_text(
        encoding="utf-8"
    )

    assert "for runtime_command in curl git wget btop python3 opencode zellij node pi; do" in template
    assert "apt-get install" not in template
    assert "nodesource.com" not in template
    assert "corepack enable" in template
    assert "pnpm add -g" not in template
    assert 'export AI_DEFAULT_PROVIDER="$${AI_DEFAULT_PROVIDER:-__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__}"' in template
    assert 'export AI_DEFAULT_MODEL="$${AI_DEFAULT_MODEL:-__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__}"' in template
    assert 'export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__}"' in template
    assert 'export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__}"' in template
    assert 'export LITELLM_DEFAULT_ALIAS="$AI_DEFAULT_PROVIDER/$AI_DEFAULT_MODEL"' in template
    assert 'export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"' in template
    assert 'with urllib.request.urlopen(request, timeout=5) as response:' in template
    assert 'payload = json.load(response)' in template
    assert 'payload = {"data": []}' in template
    assert '"baseUrl": base_url' in template
    assert '"api": "openai-completions"' in template
    assert '"apiKey": api_key' in template
    assert '"models": [{"id": model_id, "name": model_id} for model_id in model_ids]' in template
    assert 'Path("/home/coder/.pi/agent/models.json").write_text(' in template
    assert 'PI_WEB_SRC_DIR=/home/coder/.cache/pi-web-ui' in template
    assert 'PI_WEB_BUILD_KEY=v1-coder-mounted-preview' in template
    assert 'PI_WEB_UI_PORT=8650' in template
    assert 'PI_WEB_PROXY_PORT=8651' in template
    assert "Pi Web UI stays browser-local, but the workspace now pre-seeds custom LiteLLM" in template
    assert '"@earendil-works/pi-agent-core": "^0.74.0"' in template
    assert '"@earendil-works/pi-ai": "^0.74.0"' in template
    assert '"@earendil-works/pi-web-ui": "^0.74.0"' in template
    assert "import { Agent } from '@earendil-works/pi-agent-core';" in template
    assert "import { getModel } from '@earendil-works/pi-ai';" in template
    assert "import '@earendil-works/pi-web-ui/app.css';" in template
    assert 'document.title = "Pi Web UI";' in template
    assert 'CI=true pnpm install' in template
    assert 'pnpm exec vite build --base ./' in template
    assert (
        'nohup sh -lc "cd \'$PI_WEB_SRC_DIR\' && pnpm exec vite preview --host 127.0.0.1 --port $PI_WEB_UI_PORT --strictPort"'
        in template
    )
    assert "cat >/tmp/coder-mounted-proxy.mjs <<'JS'" in template
    assert 'const parsed = new URL(req.url || "/", "http://localhost");' in template
    assert 'const targetPath = needsSpaFallback(remainder) ? "/" : remainder + parsed.search;' in template
    assert (
        'nohup env SYNTHETIC_HEALTHCHECK=1 TARGET_PORT="$PI_WEB_UI_PORT" PROXY_PORT="$PI_WEB_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs'
        in template
    )
    assert 'resource "coder_app" "pi_web"' in template
    assert 'slug         = "pi-web"' in template
    assert 'display_name = "Pi Web UI"' in template
    assert 'url          = "http://localhost:8651"' in template
    assert 'share        = "owner"' in template
    assert 'subdomain    = false' in template
    assert 'url       = "http://localhost:8651/health"' in template
    assert "pi.dev/install.sh" not in template
    assert "curl | sh" not in template


def test_readme_documents_coder_litellm_scope_boundaries() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "The primary and OpenCode Web templates inherit wizard-managed LiteLLM defaults" in readme
    assert "ubuntu-vscode-openwork" in readme
    assert "ubuntu-vscode-pi-web" in readme


def _legacy_default_kdense_byok_template_includes_upstream_parameterized_stack() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'resource "coder_script" "kdense_bootstrap"' in template
    assert 'display_name       = "K-Dense BYOK Bootstrap"' in template
    assert "run_on_start       = true" in template
    assert "start_blocks_login = false" in template
    assert "timeout            = 3600" in template
    assert "cat >/tmp/kdense-bootstrap.sh <<'BOOT'" in template
    assert "chmod +x /tmp/kdense-bootstrap.sh" in template
    assert "nohup bash /tmp/kdense-bootstrap.sh >/tmp/kdense-bootstrap.log 2>&1 &" in template
    assert "for runtime_command in curl git wget btop python3 opencode zellij node; do" in template
    assert "apt-get install" not in template
    assert "astral.sh/uv/install.sh" in template
    assert "latest-v22.x" not in template
    assert "npm install -g @google/gemini-cli" in template
    assert 'data "coder_parameter" "kdense_default_model" {' in template
    assert 'data "coder_parameter" "kdense_expert_model" {' in template
    assert 'data "coder_parameter" "kdense_search_provider" {' in template
    assert 'data "coder_parameter" "kdense_opencode_go_api_key" {' in template
    assert 'data "coder_parameter" "kdense_exa_api_key" {' in template
    assert 'data "coder_parameter" "kdense_parallel_api_key" {' in template
    assert 'data "coder_parameter" "kdense_modal_token_id" {' in template
    assert 'data "coder_parameter" "kdense_modal_token_secret" {' in template
    assert 'name  = "Unsloth Active (local alias)"' in template
    assert 'value = "local-model.internal/unsloth-active"' in template
    assert 'default      = "local-model.internal/unsloth-active"' in template
    assert 'default      = "openrouter/google/gemini-3.1-pro-preview"' in template
    assert 'default      = "disabled"' in template
    assert (
        'export KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL="__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__"'
        in template
    )
    assert (
        'export KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY="__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__"'
        in template
    )
    assert "KDENSE_TEMPLATE_OPENCODE_GO_BASE_URL_PLACEHOLDER" not in template
    assert "KDENSE_TEMPLATE_OPENCODE_GO_API_KEY_PLACEHOLDER" not in template
    assert 'KDENSE_SRC_DIR=/home/coder/.cache/kdense-byok-src' in template
    assert 'git clone --depth 1 --branch main https://github.com/K-Dense-AI/k-dense-byok.git' in template
    assert '[ ! -d "$KDENSE_SRC_DIR/.venv" ]' in template
    assert '[ ! -f "$KDENSE_SRC_DIR/web/.next/BUILD_ID" ]' in template
    assert (
        'const streamdownComponents = { p: SafeParagraph } as unknown as ComponentProps<typeof Streamdown>["components"];'
        in template
    )
    assert 'status: "running" as const,' in template
    assert (
        "text = re.sub(r'status:\\s*\"running\",', 'status: \"running\" as const,', text, count=1)"
        in template
    )
    assert "text = text.replace('// @ts-expect-error polyfill\\n', '')" in template
    assert "normalize_model_for_gateway() {" in template
    assert 'openrouter/*) printf \x27openai/%s\x27 "$${model#openrouter/}" ;;' in template
    assert 'opencode-go/*) printf \x27openai/%s\x27 "$${model#opencode-go/}" ;;' in template
    assert (
        'KDENSE_DEFAULT_MODEL_EFFECTIVE=$(normalize_model_for_gateway "$KDENSE_DEFAULT_MODEL")'
        in template
    )
    assert (
        'KDENSE_EXPERT_MODEL_EFFECTIVE=$(normalize_model_for_gateway "$KDENSE_EXPERT_MODEL")'
        in template
    )
    assert "write_kdense_env_file() {" in template
    assert "DEFAULT_AGENT_MODEL=%s" in template
    assert "DEFAULT_EXPERT_MODEL=%s" in template
    assert 'append_env OPENAI_API_KEY "$KDENSE_CENTRAL_LITELLM_API_KEY" "$env_file"' in template
    assert 'append_env OPENAI_API_BASE "$KDENSE_CENTRAL_LITELLM_BASE_URL" "$env_file"' in template
    assert 'append_env OPENAI_BASE_URL "$KDENSE_CENTRAL_LITELLM_BASE_URL" "$env_file"' in template
    assert 'append_env EXA_API_KEY "$KDENSE_EXA_API_KEY" "$env_file"' in template
    assert 'append_env PARALLEL_API_KEY "$KDENSE_PARALLEL_API_KEY" "$env_file"' in template
    assert 'append_env MODAL_TOKEN_ID "$KDENSE_MODAL_TOKEN_ID" "$env_file"' in template
    assert 'append_env MODAL_TOKEN_SECRET "$KDENSE_MODAL_TOKEN_SECRET" "$env_file"' in template
    assert "append_env OPENROUTER_API_KEY " not in template
    assert "append_env NVIDIA_API_KEY " not in template
    assert "append_env ANTHROPIC_API_KEY " not in template
    assert (
        'KDENSE_CENTRAL_LITELLM_API_KEY="$${KDENSE_OPENCODE_GO_API_KEY:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY}"'
        in template
    )
    assert (
        'KDENSE_CENTRAL_LITELLM_BASE_URL="$${KDENSE_OPENCODE_GO_BASE_URL:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL}"'
        in template
    )
    assert 'KDENSE_LOCAL_LITELLM_BASE_URL="http://localhost:$KDENSE_LITELLM_PORT"' in template
    assert 'if [ -z "$KDENSE_CENTRAL_LITELLM_API_KEY" ]; then' in template
    assert (
        "KDENSE_CENTRAL_LITELLM_API_KEY is required for the central LiteLLM provider." in template
    )
    assert 'KDENSE_UPSTREAM_LITELLM="$KDENSE_SRC_DIR/litellm_config.yaml"' in template
    assert 'model_name: "openai/*"' in template
    assert "api_base: os.environ/OPENAI_API_BASE" in template
    assert "catalog_options = json.loads(sys.argv[5])" in template
    assert "for option in catalog_options:" in template
    assert 'source = dict(openrouter_models.get(option_value, {}))' in template
    assert 'clone["id"] = "openai/" + option_value[len("openrouter/"):]' in template
    assert 'clone["provider"] = "OpenCode Go"' in template
    assert "option_value.removeprefix(\"openrouter/\")" in template
    assert '"id": "openai/deepseek-v4-flash"' not in template
    assert "KDENSE_SETUP_STAMP" in template
    assert "uv sync" in template
    assert "npm ci" in template
    assert "npm install" in template
    assert "KDENSE_NEEDS_PREP=false" in template
    assert 'if [ ! -d "$KDENSE_SRC_DIR/sandbox/.gemini/skills" ]; then' in template
    assert "KDENSE_NEEDS_PREP=true" in template
    assert 'uv run python prep_sandbox.py' in template
    assert ">/tmp/kdense-prep.log 2>&1 &" in template
    assert (
        'nohup sh -lc "cd \'$KDENSE_SRC_DIR/web\' && NEXT_PUBLIC_ADK_API_URL= npm run start -- --hostname 127.0.0.1 --port $KDENSE_UI_PORT"'
        in template
    )
    assert (
        'const UI_PATHS = new Set(["/", "/favicon.ico", "/icon.png", "/site.webmanifest"]);'
        in template
    )
    assert "function isUiPath(pathname) {" in template
    assert (
        'return UI_PATHS.has(pathname) || pathname.startsWith("/_next/") || pathname.startsWith("/brand/");'
        in template
    )
    assert "function filteredHeaders(headers) {" in template
    assert 'if (["transfer-encoding", "connection"].includes(lowered)) continue;' in template
    assert "function targetForPath(pathname) {" in template
    assert "if (isUiPath(pathname)) return { host: UI_HOST, port: UI_PORT };" in template
    assert "return { host: API_HOST, port: API_PORT };" in template
    assert 'path: req.url || "/",' in template
    assert (
        "res.writeHead(upstreamRes.statusCode || 502, filteredHeaders(upstreamRes.headers));"
        in template
    )
    assert 'resource "coder_app" "kdense_byok"' in template
    assert 'display_name = "K-Dense BYOK"' in template
    assert 'icon         = "/opt/dokploy-wizard/runtime/icons/kdense.png"' not in template
    assert 'url          = "http://localhost:3001"' in template
    assert "subdomain    = true" in template
    assert 'url       = "http://localhost:3001/health"' in template


def _legacy_kdense_calls_central_litellm() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    assert (
        'export KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL="__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__"'
        in template
    )
    assert (
        'export KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY="__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__"'
        in template
    )
    assert (
        'KDENSE_CENTRAL_LITELLM_BASE_URL="$${KDENSE_OPENCODE_GO_BASE_URL:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL}"'
        in template
    )
    assert (
        'KDENSE_CENTRAL_LITELLM_API_KEY="$${KDENSE_OPENCODE_GO_API_KEY:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY}"'
        in template
    )
    assert 'KDENSE_LOCAL_LITELLM_BASE_URL="http://localhost:$KDENSE_LITELLM_PORT"' in template
    assert (
        'printf \'GOOGLE_GEMINI_BASE_URL=%s\\n\' "$KDENSE_LOCAL_LITELLM_BASE_URL" >> "$env_file"'
        in template
    )
    assert 'append_env OPENAI_API_KEY "$KDENSE_CENTRAL_LITELLM_API_KEY" "$env_file"' in template
    assert 'append_env OPENAI_API_BASE "$KDENSE_CENTRAL_LITELLM_BASE_URL" "$env_file"' in template
    assert 'append_env OPENAI_BASE_URL "$KDENSE_CENTRAL_LITELLM_BASE_URL" "$env_file"' in template


def _legacy_no_openrouter_wildcard_in_kdense_config() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    assert "# Central LiteLLM gateway owns the OpenCode Go wildcard route." in template
    assert (
        "# Workspace-local LiteLLM stays on localhost for the Gemini/OpenAI shim only." in template
    )
    assert "KDENSE_OPENROUTER_API_KEY" not in template
    assert "kdense_openrouter_api_key" not in template
    assert 'model_name: "openrouter/*"' not in template
    assert 'model_name: "openai/*"' in template


def _legacy_kdense_template_preserves_restored_byok_source_state() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'data "coder_parameter" "kdense_opencode_go_base_url" {' in template
    assert 'display_name = "Central LiteLLM Base URL"' in template
    assert 'default      = ""' in template
    assert (
        'KDENSE_CENTRAL_LITELLM_BASE_URL="$${KDENSE_OPENCODE_GO_BASE_URL:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL}"'
        in template
    )
    assert "KDENSE_TEMPLATE_OPENCODE_GO_BASE_URL_PLACEHOLDER" not in template
    assert "KDENSE_TEMPLATE_OPENCODE_GO_API_KEY_PLACEHOLDER" not in template
    assert 'append_env OPENROUTER_API_KEY ' not in template
    assert 'append_env NVIDIA_API_KEY ' not in template
    assert 'append_env ANTHROPIC_API_KEY ' not in template


def _legacy_hermes_template_includes_full_web_stack() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-hermes/main.tf").read_text(
        encoding="utf-8"
    )

    assert "for runtime_command in curl git wget btop python3 opencode zellij node; do" in template
    assert "apt-get install" not in template
    assert "nodesource.com" not in template
    assert "export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in template
    assert (
        'export HERMES_TEMPLATE_PROVIDER="__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__"' in template
    )
    assert 'export HERMES_TEMPLATE_MODEL="__DOKPLOY_WIZARD_HERMES_MODEL__"' in template
    assert 'export HERMES_TEMPLATE_BASE_URL="__DOKPLOY_WIZARD_HERMES_BASE_URL__"' in template
    assert 'export HERMES_TEMPLATE_API_KEY="$${LITELLM_VIRTUAL_KEY_CODER_HERMES}"' in template
    assert (
        'export HERMES_TEMPLATE_API_KEY_PLACEHOLDER="__DOKPLOY_WIZARD_HERMES_API_KEY_PLACEHOLDER__"' in template
    )
    assert (
        'export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"'
        in template
    )
    assert (
        'export HERMES_INFERENCE_PROVIDER="$${HERMES_INFERENCE_PROVIDER:-$HERMES_TEMPLATE_PROVIDER}"'
        in template
    )
    assert 'export HERMES_MODEL="$${HERMES_MODEL:-$HERMES_TEMPLATE_MODEL}"' in template
    assert 'export OPENAI_API_BASE="$${OPENAI_API_BASE:-$HERMES_TEMPLATE_BASE_URL}"' in template
    assert 'export OPENAI_API_KEY="$${OPENAI_API_KEY:-$HERMES_TEMPLATE_API_KEY}"' in template
    assert 'export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-$OPENAI_API_BASE}"' in template
    assert 'export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-$OPENAI_API_KEY}"' in template
    assert (
        'export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$AI_DEFAULT_BASE_URL}"' in template
    )
    assert 'export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$AI_DEFAULT_API_KEY}"' in template
    assert 'upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"' in template
    assert 'upsert_env OPENAI_API_BASE "$OPENAI_API_BASE"' in template
    assert 'upsert_env AI_DEFAULT_API_KEY "$AI_DEFAULT_API_KEY"' in template
    assert 'upsert_env OPENCODE_GO_API_KEY "$OPENCODE_GO_API_KEY"' in template
    assert "upsert_env OPENROUTER_API_KEY " not in template
    assert "upsert_env NVIDIA_API_KEY " not in template
    assert "upsert_env ANTHROPIC_API_KEY " not in template
    assert "API_SERVER_ENABLED=true" in template
    assert "OPENAI_API_KEY is required for the Hermes workspace template" in template
    assert 'hermes config set model.provider "$HERMES_INFERENCE_PROVIDER"' in template
    assert 'hermes config set model.default "$HERMES_MODEL"' in template
    assert 'hermes config set model.base_url "$OPENAI_API_BASE"' in template
    assert "hermes config set terminal.cwd /home/coder" in template
    assert 'providers[provider] = {' in template
    assert '"name": "Dokploy LiteLLM"' in template
    assert '"base_url": base_url' in template
    assert '"models": {model_id: {} for model_id in model_ids}' in template
    assert '"discover_models": False' in template
    assert "export HERMES_DASHBOARD_PORT=9119" in template
    assert "export HERMES_DASHBOARD_PROXY_PORT=9120" in template
    assert "export HERMES_WEB_UI_PORT=8648" in template
    assert "export HERMES_WEB_UI_PROXY_PORT=8649" in template
    assert "export HERMES_WEBUI_PORT=8787" in template
    assert "export HERMES_WEBUI_PROXY_PORT=8788" in template
    assert "HERMES_BOOTSTRAP_SCRIPT=/tmp/hermes-workspace-bootstrap.sh" in template
    assert 'export HERMES_HOME="$${HERMES_HOME:-/home/coder/.hermes}"' in template
    assert 'export HERMES_INSTALL_DIR="$${HERMES_INSTALL_DIR:-/home/coder/.hermes/hermes-agent}"' in template
    assert 'nohup sh "$HERMES_BOOTSTRAP_SCRIPT" >/tmp/hermes-bootstrap.log 2>&1 &' in template
    assert "nohup hermes gateway >/tmp/hermes-gateway.log 2>&1 &" in template
    assert 'hermes dashboard --host 127.0.0.1 --port "$HERMES_DASHBOARD_PORT" --no-open' in template
    assert (
        'hermes-web-ui start --port "$HERMES_WEB_UI_PORT" >/tmp/hermes-web-ui-start.log 2>&1'
        in template
    )
    assert (
        "HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=$HERMES_WEBUI_PORT HERMES_WEBUI_AGENT_DIR=$HERMES_INSTALL_DIR"
        in template
    )
    assert "python3 /home/coder/.cache/hermes-webui-src/bootstrap.py --no-browser --skip-agent-install" in template
    assert 'const SYNTHETIC_HEALTHCHECK = process.env.SYNTHETIC_HEALTHCHECK === "1";' in template
    assert (
        'const DASHBOARD_SESSION_HEADER = process.env.DASHBOARD_SESSION_HEADER === "1";' in template
    )
    assert 'const TOKEN_FILE = process.env.TOKEN_FILE || "";' in template
    assert 'headers["X-Hermes-Session-Token"] = await getDashboardSessionToken();' in template
    assert (
        "window.history.pushState = (state, title, url) => originalPushState(state, title, url == null ? url : rewrite(url));"
        in template
    )
    assert 'location.pathname.indexOf("/apps/") !== -1' in template
    assert '.replace(/(["\'])\\/assets\\//g, "$1./assets/")' in template
    assert '.replace(/(["\'])\\/static\\//g, "$1./static/")' in template
    assert '.replace(/`\\/`\\+e/g, "`./`+e")' in template
    assert (
        "DASHBOARD_SESSION_HEADER=1 SYNTHETIC_HEALTHCHECK=1 TARGET_PORT=$HERMES_DASHBOARD_PORT PROXY_PORT=$HERMES_DASHBOARD_PROXY_PORT node /tmp/coder-mounted-proxy.mjs"
        in template
    )
    assert (
        "TOKEN_FILE=/home/coder/.hermes-web-ui/.token TARGET_PORT=$HERMES_WEB_UI_PORT PROXY_PORT=$HERMES_WEB_UI_PROXY_PORT node /tmp/coder-mounted-proxy.mjs"
        in template
    )
    assert 'server.on("upgrade", (req, socket, head) => {' in template
    assert "window.WebSocket = class extends OriginalWebSocket" in template
    assert 'resource "coder_app" "hermes_dashboard"' in template
    assert 'icon         = "/opt/dokploy-wizard/runtime/icons/hermes.svg"' not in template
    assert 'url          = "http://localhost:9120"' in template
    assert 'resource "coder_app" "hermes_web_ui"' in template
    assert 'icon         = "/opt/dokploy-wizard/runtime/icons/hermes-web.svg"' not in template
    assert 'url          = "http://localhost:8649"' in template
    assert 'resource "coder_app" "hermes_webui"' in template
    assert '/opt/dokploy-wizard/runtime/icons/' not in template
    assert 'url          = "http://localhost:8788"' in template
    assert "HERMIES_PROVIDER" not in template
    assert "HERMEIS_OPENCODE_GO_MODEL" not in template
    assert "HERMIES_BASE_USL" not in template
    assert "HERMIES_API_MODE" not in template


def test_hermes_dashboard_classic_webui_removal_preserves_immutable_pins() -> None:
    # Given
    template = Path("templates/coder/default-ubuntu-code-server-hermes/main.tf").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        Path("src/dokploy_wizard/runtime-manifest.lock.json").read_text(encoding="utf-8")
    )

    # When
    hermes = manifest["workspace_runtime"]["hermes"]

    # Then
    assert "hermes-web-ui" not in template
    assert "HERMES_WEB_UI_PORT" not in template
    assert "HERMES_WEB_UI_PROXY_PORT" not in template
    assert "8648" not in template
    assert "8649" not in template
    assert "export HERMES_DASHBOARD_PORT=9119" in template
    assert "export HERMES_DASHBOARD_PROXY_PORT=9120" in template
    assert "export HERMES_WEBUI_PORT=8787" in template
    assert "export HERMES_WEBUI_PROXY_PORT=8788" in template
    assert 'url          = "http://localhost:9120"' in template
    assert 'url          = "http://localhost:8788"' in template
    assert "coder workspace" not in template
    assert "PIP_NO_INDEX=1" in template
    assert "--skip-agent-install" in template
    assert 'PYTHONPATH="$HERMES_SOURCE:$HERMES_CLASSIC" "$HERMES_VENV/bin/python" "$HERMES_CLASSIC/bootstrap.py"' in template
    assert "uv python install" not in template
    assert hermes["source"] == {
        "archive_sha256": "2a9cd3f205b9e0df5a687fe4cd61ac082a14d40ddc55149ee35b2b5093ab01d3",
        "archive_url": "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/a7d7c02cb6db071eced4ac82e24f878588619600",
        "commit": "a7d7c02cb6db071eced4ac82e24f878588619600",
        "repository": "NousResearch/hermes-agent",
    }
    assert hermes["classic"] == {
        "archive_sha256": "cd5f5d40ca5ad77336eb8048b226b05279721eace5cf14f8ca1cdb0513cb7662",
        "archive_url": "https://codeload.github.com/nesquena/hermes-webui/tar.gz/4d9965b37a5ca3dbec1c19ccdaa261211b180804",
        "commit": "4d9965b37a5ca3dbec1c19ccdaa261211b180804",
        "repository": "nesquena/hermes-webui",
        "tree": "a9436fa15d5752bfbf20e281fe54f39869ea5388",
    }
    assert hermes["tools"]["cpython"] == {
        "amd64": {
            "sha256": "17e1f5b2c9668217ba554decd94db04adf3d085203d322c690fd44cde549d576",
            "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20260303/cpython-3.11.15%2B20260303-x86_64-unknown-linux-gnu-install_only.tar.gz",
        },
        "arm64": {
            "sha256": "4786f5ef8567c982517fcd7cb189a5ef4a712ed0aaf3034e839749794155ad4c",
            "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20260303/cpython-3.11.15%2B20260303-aarch64-unknown-linux-gnu-install_only.tar.gz",
        },
        "version": "3.11.15+20260303",
    }
    assert hermes["tools"]["uv"] == {
        "amd64": {
            "sha256": "04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829",
            "url": "https://github.com/astral-sh/uv/releases/download/0.11.29/uv-x86_64-unknown-linux-gnu.tar.gz",
        },
        "arm64": {
            "sha256": "94500fb064ae3c971a873cba64d94694c50677e0a4dbf78735c80509e7429919",
            "url": "https://github.com/astral-sh/uv/releases/download/0.11.29/uv-aarch64-unknown-linux-gnu.tar.gz",
        },
        "version": "0.11.29",
    }
    assert hermes["python_packages"]["pyyaml"] == {
        "cp311": {
            "amd64": "b8bb0864c5a28024fac8a632c443c87c5aa6f215c0b126c449ae1a150412f31d",
            "arm64": "10892704fc220243f5305762e276552a0395f7beb4dbf9b14ec8fd43b57f126c",
        },
        "version": "6.0.3",
    }


def test_hermes_webui_icons_are_vendored_from_pinned_archives() -> None:
    # Given
    template_root = Path("templates/coder/default-ubuntu-code-server-hermes")
    template = (template_root / "main.tf").read_text(encoding="utf-8")
    manifest = json.loads(
        Path("src/dokploy_wizard/runtime-manifest.lock.json").read_text(encoding="utf-8")
    )
    expected = {
        "dashboard": {
            "archive": "source",
            "path": "acp_registry/icon.svg",
            "sha256": "8f6157ebb2ca034bec36094205e47777c794f9586fe33b6ca7e11e5a0a0dd6a3",
        },
        "separate_webui": {
            "archive": "source",
            "path": "website/static/img/favicon.svg",
            "sha256": "c4d55805bda8e16072ed77c0725176ab2218a9e628d1ba776a6048a39c28f751",
        },
        "classic": {
            "archive": "classic",
            "path": "static/favicon.svg",
            "sha256": "b883cc5f4e2fa5ee01cc87e1ed1546ba9ab47079632ce692a77cfa65178bb6d6",
        },
    }

    # When
    icons = manifest["workspace_runtime"]["hermes"]["icons"]

    # Then
    assert icons == expected
    icon_files = {
        "dashboard": template_root / ".dokploy-wizard/icons/hermes-dashboard.svg",
        "separate_webui": template_root / ".dokploy-wizard/icons/hermes-webui.svg",
        "classic": template_root / ".dokploy-wizard/icons/hermes-classic.svg.b64",
    }
    icon_bytes = {
        name: b64decode(path.read_bytes()) if name == "classic" else path.read_bytes()
        for name, path in icon_files.items()
    }
    assert {name: sha256(content).hexdigest() for name, content in icon_bytes.items()} == {
        name: record["sha256"] for name, record in expected.items()
    }
    with coder_module._rendered_template_dir(
        template_dir=template_root, replacements=None
    ) as rendered:
        assert all(
            (rendered / path.relative_to(template_root)).is_file()
            for path in icon_files.values()
        )
    assert 'icon         = format("data:image/svg+xml;base64,%s", filebase64("${path.module}/.dokploy-wizard/icons/hermes-dashboard.svg"))' in template
    assert 'icon         = format("data:image/svg+xml;base64,%s", trimspace(file("${path.module}/.dokploy-wizard/icons/hermes-classic.svg.b64")))' in template


def test_hermes_yaml_dependency_failure_is_prevented_by_the_locked_venv() -> None:
    # Given
    template = Path("templates/coder/default-ubuntu-code-server-hermes/main.tf").read_text(
        encoding="utf-8"
    )

    # When / Then
    assert '"$HERMES_UV" venv --python "$HERMES_PYTHON" "$HERMES_VENV"' in template
    assert '"$HERMES_UV" sync --locked --extra all --python "$HERMES_PYTHON"' in template
    assert 'import sys, yaml; assert sys.version_info[:3] == (3, 11, 15); assert yaml.__version__ == "6.0.3"' in template


def test_hermes_template_uses_litellm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="clayton@openmerge.me",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        hermes_inference_provider="openai",
        hermes_model="unsloth-active",
        ai_default_base_url="https://upstream.example.invalid/v1",
        ai_default_api_key="litellm-coder-hermes-key",
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    template_replacements_by_name: dict[str, dict[str, str] | None] = {}
    _patch_template_migration(monkeypatch, template_replacements_by_name)
    secret_sync_calls: list[dict[str, object]] = []
    _patch_workspace_runtime_image_replacements(monkeypatch, backend)

    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: False)
    monkeypatch.setattr(coder_module, "_create_coder_first_user", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_template_version_names", lambda **kwargs: ())
    monkeypatch.setattr(
        coder_module,
        "_sync_hermes_workspace_secrets",
        lambda **kwargs: secret_sync_calls.append(kwargs),
    )
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda *,
            container_name,
            template_dir,
            template_name,
            replacements: template_replacements_by_name.setdefault(
                template_name, _without_runtime_image_replacements(replacements)
            ),
    )
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)

    backend.ensure_application_ready()

    assert secret_sync_calls == []
    assert template_replacements_by_name[coder_module._default_hermes_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__": "openai",
        "__DOKPLOY_WIZARD_HERMES_MODEL__": "local-model.internal/unsloth-active",
        "__DOKPLOY_WIZARD_HERMES_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }


def test_base_opencode_web_openwork_templates_receive_shared_litellm_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="clayton@openmerge.me",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        hermes_inference_provider="openai",
        hermes_model="unsloth-active",
        ai_default_api_key="litellm-coder-hermes-key",
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    template_replacements_by_name: dict[str, dict[str, str] | None] = {}
    _patch_template_migration(monkeypatch, template_replacements_by_name)
    _patch_workspace_runtime_image_replacements(monkeypatch, backend)

    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: False)
    monkeypatch.setattr(coder_module, "_create_coder_first_user", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_template_version_names", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda *,
            container_name,
            template_dir,
            template_name,
            replacements: template_replacements_by_name.setdefault(
                template_name, _without_runtime_image_replacements(replacements)
            ),
    )
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)

    backend.ensure_application_ready()

    assert template_replacements_by_name[coder_module._default_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_opencode_web_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_kdense_byok_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4-flash",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__": "$${LITELLM_VIRTUAL_KEY_CODER_KDENSE}",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }


def test_ensure_application_ready_runs_template_migration_for_healthy_existing_coder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="clayton@openmerge.me",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        ai_default_api_key="litellm-coder-hermes-key",
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    template_replacements_by_name: dict[str, dict[str, str] | None] = {}
    migration_calls = _patch_template_migration(monkeypatch, template_replacements_by_name)
    template_push_calls: list[str] = []
    ensure_workspace_calls: list[object] = []
    _patch_workspace_runtime_image_replacements(monkeypatch, backend)

    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(
        backend,
        "_verify_current_compose_application",
        lambda: type("HealthyResult", (), {"passed": True})(),
    )
    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_template_version_names", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda *,
            container_name,
            template_dir,
            template_name,
            replacements: template_replacements_by_name.setdefault(
                template_name, _without_runtime_image_replacements(replacements)
            ),
    )
    monkeypatch.setattr(
        coder_module,
        "_push_default_template",
        lambda *, template_name, **kwargs: template_push_calls.append(template_name),
    )
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: ensure_workspace_calls.append(kwargs),
    )

    notes = backend.ensure_application_ready()

    assert len(migration_calls) == 1
    assert tuple(source.name for source in migration_calls[0].sources) == (
        coder_module._required_template_names()
    )
    assert template_push_calls == []
    assert ensure_workspace_calls == []
    assert notes == ()
    assert template_replacements_by_name[coder_module._default_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_opencode_web_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }


def test_push_default_template_requires_terraform_lockfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    coder_module._push_default_template(
        container_name="coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        template_name="ubuntu-vscode-opencode-web",
    )

    assert calls == [
        [
            "docker",
            "exec",
            "-e",
            "CODER_URL=http://127.0.0.1:3000",
            "-e",
            "CODER_SESSION_TOKEN=session-123",
            "coder-container",
            "/opt/coder",
            "templates",
            "push",
            "ubuntu-vscode-opencode-web",
            "--directory",
            "/tmp/ubuntu-vscode-opencode-web",
            "--yes",
        ]
    ]


def test_push_default_template_treats_duplicate_deterministic_version_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_version_name = "dokploy-wizard-0a966b668508e2d3"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr=(
                f'error: A template version with name "{template_version_name}" '
                "already exists for this template."
            ),
        )

    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    coder_module._push_default_template(
        container_name="coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        template_name="ubuntu-vscode-opencode-web",
        template_version_name=template_version_name,
    )


def test_push_default_template_raises_for_unrelated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="error: failed to reach provisioner registry",
        )

    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    with pytest.raises(coder_module.CoderError, match="failed to reach provisioner registry"):
        coder_module._push_default_template(
            container_name="coder-container",
            hostname="coder.example.com",
            session_token="session-123",
            template_name="ubuntu-vscode-opencode-web",
            template_version_name="dokploy-wizard-0a966b668508e2d3",
        )


def test_reconcile_coder_creates_service_and_data() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_CODER": "true",
            },
        )
    )
    phase = reconcile_coder(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeCoderBackend(),
    )

    assert phase.result.outcome == "applied"
    assert phase.result.hostname == "coder.example.com"
    assert phase.result.wildcard_hostname == "*.example.com"
    assert phase.service_resource_id == "coder-service-1"
    assert phase.data_resource_id == "coder-data-1"
    assert phase.result.config is not None
    assert phase.result.config.wildcard_access_url == "*.example.com"


def test_reconcile_coder_runs_application_bootstrap_before_final_health_gate_on_first_apply() -> (
    None
):
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "ROOT_DOMAIN": "example.com",
            "STACK_NAME": "wizard-stack",
            "PACKS": "coder",
            "DOKPLOY_API_URL": "https://dokploy.example.com/api",
            "DOKPLOY_API_KEY": "key-123",
            "DOKPLOY_ADMIN_EMAIL": "clayton@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "ChangeMeSoon",
        },
    )
    desired_state = resolve_desired_state(raw_env)
    backend = FakeCoderBackend(health_ok=True, health_results=[False, True])

    phase = reconcile_coder(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert backend.ensure_calls == 1
    assert phase.result.outcome == "applied"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True


def test_ensure_application_ready_waits_for_first_user_endpoint_on_fresh_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    backend._created_in_process = True
    _patch_workspace_runtime_image_replacements(monkeypatch, backend)
    migration_calls = _patch_template_migration(monkeypatch)

    waits: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_wait_for_coder_bootstrap_api_ready",
        lambda hostname: waits.append(hostname),
    )
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: False)
    monkeypatch.setattr(coder_module, "_create_coder_first_user", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-token")
    monkeypatch.setattr(
        coder_module, "_coder_container_name", lambda service_name: "coder-container"
    )
    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_template_version_names", lambda **kwargs: ())
    secret_sync_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        coder_module,
        "_sync_hermes_workspace_secrets",
        lambda **kwargs: secret_sync_calls.append(
            (
                str(kwargs["hermes_inference_provider"]),
                str(kwargs["hermes_model"]),
                str(kwargs["ai_default_base_url"]),
            )
        ),
    )
    monkeypatch.setattr(coder_module, "_copy_template_into_container", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)

    notes = backend.ensure_application_ready()

    assert waits == ["coder.example.com"]
    assert len(migration_calls) == 1
    assert secret_sync_calls == []
    assert notes == ("Provisioned initial Coder admin for 'admin@example.com'.",)


def test_ensure_application_ready_is_idempotent_on_second_bootstrap_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="clayton@openmerge.me",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    first_user_exists = False
    first_user_calls: list[tuple[str, str, str]] = []
    template_versions: dict[str, str] = {}
    template_copy_calls: list[str] = []
    template_push_calls: list[tuple[str, str | None]] = []
    created_workspaces: list[tuple[str, str]] = []
    workspaces: set[str] = set()
    _patch_workspace_runtime_image_replacements(monkeypatch, backend)
    migration_calls: list[ProductionMigrationInputs] = []

    def fake_template_migration(inputs: ProductionMigrationInputs) -> None:
        if not migration_calls:
            for source in inputs.sources:
                template_copy_calls.append(source.name)
                template_push_calls.append(
                    (
                        source.name,
                        inputs.version_name(
                            template_dir=source.directory,
                            replacements=dict(source.replacements),
                        ),
                    )
                )
        migration_calls.append(inputs)

    monkeypatch.setattr(coder_module, "execute_template_migration", fake_template_migration)

    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: first_user_exists)

    def fake_create_first_user(*, hostname: str, email: str, password: str) -> None:
        nonlocal first_user_exists
        first_user_calls.append((hostname, email, password))
        first_user_exists = True

    monkeypatch.setattr(coder_module, "_create_coder_first_user", fake_create_first_user)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(
        coder_module,
        "_active_template_version_name",
        lambda **kwargs: template_versions.get(str(kwargs["template_name"])),
    )
    monkeypatch.setattr(
        coder_module,
        "_template_version_names",
        lambda **kwargs: tuple(template_versions.values()),
    )
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda *, template_name, **kwargs: template_copy_calls.append(template_name),
    )

    def fake_push_default_template(*, template_name: str, template_version_name: str | None = None, **kwargs: object) -> None:
        template_push_calls.append((template_name, template_version_name))
        if template_version_name is not None:
            template_versions[template_name] = template_version_name

    monkeypatch.setattr(coder_module, "_push_default_template", fake_push_default_template)
    monkeypatch.setattr(
        coder_module,
        "_default_workspace_name",
        lambda hostname: "openmergeme-workspace-2026-04-18",
    )
    monkeypatch.setattr(coder_module, "_list_workspaces", lambda **kwargs: tuple(sorted(workspaces)))

    def fake_create_default_workspace(*, workspace_name: str, template_name: str, **kwargs: object) -> None:
        created_workspaces.append((workspace_name, template_name))
        workspaces.add(workspace_name)

    monkeypatch.setattr(coder_module, "_create_default_workspace", fake_create_default_workspace)

    first_notes = backend.ensure_application_ready()
    second_notes = backend.ensure_application_ready()

    expected_template_names = set(coder_module._required_template_names())
    assert first_user_calls == [("coder.example.com", "clayton@openmerge.me", "ChangeMeSoon")]
    assert len(migration_calls) == 2
    assert set(template_copy_calls) == expected_template_names
    assert len(template_copy_calls) == len(expected_template_names)
    assert {name for name, _ in template_push_calls} == expected_template_names
    assert len(template_push_calls) == len(expected_template_names)
    assert all(version_name and version_name.startswith("dokploy-wizard-") for _, version_name in template_push_calls)
    assert created_workspaces == [
        ("openmergeme-workspace-2026-04-18", coder_module._default_template_name())
    ]
    assert first_notes == (
        "Provisioned initial Coder admin for 'clayton@openmerge.me'.",
        "Created default Coder workspace 'openmergeme-workspace-2026-04-18' for 'clayton@openmerge.me'.",
    )
    assert second_notes == ()


def _coder_backend_for_template_failure_tests() -> DokployCoderBackend:
    return DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="clayton@openmerge.me",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        ai_default_api_key="litellm-coder-hermes-key",
        client=cast(DokployCoderApi, FakeCoderApi()),
    )


def _patch_coder_template_failure_bootstrap(
    monkeypatch: pytest.MonkeyPatch, backend: DokployCoderBackend
) -> None:
    backend._created_in_process = True
    _patch_workspace_runtime_image_replacements(monkeypatch, backend)
    monkeypatch.setattr(coder_module, "_wait_for_coder_bootstrap_api_ready", lambda hostname: None)
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)


def test_template_migration_blocker_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _coder_backend_for_template_failure_tests()
    _patch_coder_template_failure_bootstrap(monkeypatch, backend)

    def fail_migration(inputs: ProductionMigrationInputs) -> None:
        del inputs
        raise TemplateMigrationExecutionError(
            "blocked migration",
            code="CODER_RETIRED_WORKSPACE_NOT_STOPPED",
        )

    monkeypatch.setattr(coder_module, "execute_template_migration", fail_migration)

    with pytest.raises(
        coder_module.CoderError,
        match="migration failed closed.*CODER_RETIRED_WORKSPACE_NOT_STOPPED",
    ):
        backend.ensure_application_ready()


def test_template_migration_runs_after_secret_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _coder_backend_for_template_failure_tests()
    _patch_coder_template_failure_bootstrap(monkeypatch, backend)
    events: list[str] = []
    monkeypatch.setattr(
        backend,
        "_reconcile_coder_workspace_secrets",
        lambda **kwargs: events.append("secrets"),
    )
    monkeypatch.setattr(
        coder_module,
        "execute_template_migration",
        lambda inputs: events.append("migration"),
    )

    backend.ensure_application_ready()

    assert events == ["secrets", "migration"]


def test_seed_template_skips_push_when_desired_version_is_already_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "main.tf").write_text("resource \"x\" \"y\" {}\n", encoding="utf-8")
    desired_version_name = coder_module._template_version_name(
        template_dir=template_dir,
        replacements=None,
    )
    copy_calls: list[str] = []
    push_calls: list[str] = []

    monkeypatch.setattr(
        coder_module,
        "_active_template_version_name",
        lambda **kwargs: desired_version_name,
    )
    monkeypatch.setattr(
        coder_module,
        "_template_version_names",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not list versions")),
    )
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda **kwargs: copy_calls.append("copy"),
    )
    monkeypatch.setattr(
        coder_module,
        "_push_default_template",
        lambda **kwargs: push_calls.append("push"),
    )

    seeded = coder_module._seed_template(
        container_name="coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        template_name="ubuntu-vscode",
        template_dir=template_dir,
        replacements=None,
    )

    assert seeded is False
    assert copy_calls == []
    assert push_calls == []


def test_seed_template_skips_push_when_desired_version_already_exists_but_is_not_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "main.tf").write_text("resource \"x\" \"y\" {}\n", encoding="utf-8")
    desired_version_name = coder_module._template_version_name(
        template_dir=template_dir,
        replacements=None,
    )
    copy_calls: list[str] = []
    push_calls: list[str] = []

    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: "older-version")
    monkeypatch.setattr(
        coder_module,
        "_template_version_names",
        lambda **kwargs: ("older-version", desired_version_name),
    )
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda **kwargs: copy_calls.append("copy"),
    )
    monkeypatch.setattr(
        coder_module,
        "_push_default_template",
        lambda **kwargs: push_calls.append("push"),
    )

    seeded = coder_module._seed_template(
        container_name="coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        template_name="ubuntu-vscode",
        template_dir=template_dir,
        replacements=None,
    )

    assert seeded is False
    assert copy_calls == []
    assert push_calls == []


def test_seed_template_pushes_when_desired_version_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "main.tf").write_text("resource \"x\" \"y\" {}\n", encoding="utf-8")
    copy_calls: list[str] = []
    push_calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: "older-version")
    monkeypatch.setattr(
        coder_module,
        "_template_version_names",
        lambda **kwargs: ("older-version",),
    )
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda **kwargs: copy_calls.append(str(kwargs["template_name"])),
    )
    monkeypatch.setattr(
        coder_module,
        "_push_default_template",
        lambda **kwargs: push_calls.append(
            (str(kwargs["template_name"]), kwargs.get("template_version_name"))
        ),
    )

    seeded = coder_module._seed_template(
        container_name="coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        template_name="ubuntu-vscode",
        template_dir=template_dir,
        replacements=None,
    )

    stderr = capsys.readouterr().err

    assert seeded is True
    assert copy_calls == ["ubuntu-vscode"]
    assert push_calls == [("ubuntu-vscode", push_calls[0][1])]
    assert push_calls[0][1] is not None
    assert push_calls[0][1].startswith("dokploy-wizard-")
    assert "[dokploy-wizard] Checking Coder template 'ubuntu-vscode'." in stderr
    assert "Pushing Coder template 'ubuntu-vscode' as version 'dokploy-wizard-" in stderr
    assert "[dokploy-wizard] Finished Coder template 'ubuntu-vscode' push." in stderr
    assert "session-123" not in stderr


def test_build_coder_ledger_replaces_existing_resources() -> None:
    ledger = build_coder_ledger(
        existing_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type="coder_service",
                    resource_id="old-service",
                    scope="stack:wizard-stack:coder:service",
                ),
                OwnedResource(
                    resource_type="coder_data",
                    resource_id="old-data",
                    scope="stack:wizard-stack:coder:data",
                ),
            ),
        ),
        stack_name="wizard-stack",
        service_resource_id="new-service",
        data_resource_id="new-data",
    )

    assert {(item.resource_type, item.resource_id) for item in ledger.resources} == {
        ("coder_service", "new-service"),
        ("coder_data", "new-data"),
    }


def test_dokploy_coder_backend_renders_compose_on_create() -> None:
    api = FakeCoderApi()
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, api),
    )

    record = backend.create_service(
        resource_name="wizard-stack-coder",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        data_resource_name="wizard-stack-coder-data",
    )

    assert record.resource_name == "wizard-stack-coder"
    compose = api.last_create_compose_file
    assert compose is not None
    assert 'CODER_ACCESS_URL: "https://coder.example.com/"' in compose
    assert 'CODER_WILDCARD_ACCESS_URL: "*.coder.example.com"' in compose


def test_dokploy_coder_backend_skips_healthy_unchanged_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered_compose = _render_compose_file(
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
                password_secret_ref="wizard-stack-coder-postgres-password",
            ),
        )
    compose = rendered_compose.compose_file
    _write_coder_hash_checkpoint(
        tmp_path,
        service_name="wizard-stack-coder",
        compose_file=rendered_compose,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name="wizard-stack-coder",
        compose_id="cmp-coder",
        project_name="wizard-stack",
        compose_file=compose,
    )
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        state_dir=tmp_path,
        client=cast(DokployCoderApi, client),
    )
    wait_calls: list[str] = []
    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: True)
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(
        coder_module,
        "_wait_for_coder_bootstrap_api_ready",
        lambda hostname: wait_calls.append(hostname),
    )
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_list_templates",
        lambda **kwargs: tuple(
            {"name": template_name}
            for template_name in coder_module._required_template_names()
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: (coder_module._default_workspace_name("coder.example.com"),),
    )

    record = backend.create_service(
        resource_name="wizard-stack-coder",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        data_resource_name="wizard-stack-coder-data",
    )

    assert record == CoderResourceRecord(
        resource_id="dokploy-compose:cmp-coder:service",
        resource_name="wizard-stack-coder",
    )
    assert backend._created_in_process is False
    assert wait_calls == ["coder.example.com"]
    client.assert_unchanged_service("wizard-stack-coder")


def test_dokploy_coder_verification_confirms_bootstrap_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: True)
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(coder_module, "_wait_for_coder_bootstrap_api_ready", lambda hostname: None)
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_list_templates",
        lambda **kwargs: tuple(
            {"name": template_name}
            for template_name in coder_module._required_template_names()
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: (coder_module._default_workspace_name("coder.example.com"),),
    )

    result = backend._verify_current_compose_application()

    assert result.passed is True
    assert result.tier == "bootstrap"
    assert "first user bootstrap" in result.detail
    assert "seeded templates" in result.detail
    assert "default workspace" in result.detail


def test_dokploy_coder_verification_resolves_dokploy_prefixed_container_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="openmerge",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="openmerge-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="openmerge_coder",
            user_name="openmerge_coder",
            password_secret_ref="openmerge-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    exec_container_names: list[str] = []

    def fake_run(
        command: list[str], check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="openmerge-coder-ofbxpg-openmerge-coder-1\n",
                stderr="",
            )
        if command[:4] == ["docker", "exec", "-e", f"CODER_URL={coder_module._coder_cli_url()}"]:
            exec_container_names.append(command[6])
            if command[7:10] == ["/opt/coder", "templates", "list"]:
                stdout = json.dumps(
                    [
                        {"name": template_name}
                        for template_name in coder_module._required_template_names()
                    ]
                )
            elif command[7:9] == ["/opt/coder", "list"]:
                stdout = json.dumps(
                    [{"name": coder_module._default_workspace_name("coder.example.com")}]
                )
            else:
                raise AssertionError(f"Unexpected docker exec command: {command}")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=stdout,
                stderr="",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: True)
    monkeypatch.setattr(coder_module, "_wait_for_coder_bootstrap_api_ready", lambda hostname: None)
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    result = backend._verify_current_compose_application()

    assert result.passed is True
    assert exec_container_names == [
        "openmerge-coder-ofbxpg-openmerge-coder-1",
        "openmerge-coder-ofbxpg-openmerge-coder-1",
    ]


def test_dokploy_coder_backend_unhealthy_api_blocks_noop_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = _render_compose_file(
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
    ).compose_file
    _write_coder_hash_checkpoint(
        tmp_path,
        service_name="wizard-stack-coder",
        compose_file=compose,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name="wizard-stack-coder",
        compose_id="cmp-coder",
        project_name="wizard-stack",
        compose_file=compose,
    )
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        state_dir=tmp_path,
        client=cast(DokployCoderApi, client),
    )
    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: True)
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )

    def fail_api_ready(hostname: str) -> None:
        raise coder_module.CoderError(
            "Coder bootstrap API did not become ready before first-user setup."
        )

    monkeypatch.setattr(coder_module, "_wait_for_coder_bootstrap_api_ready", fail_api_ready)

    record = backend.create_service(
        resource_name="wizard-stack-coder",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        data_resource_name="wizard-stack-coder-data",
    )

    assert record == CoderResourceRecord(
        resource_id="dokploy-compose:cmp-coder:service",
        resource_name="wizard-stack-coder",
    )
    assert backend._created_in_process is True
    client.assert_single_update_deploy_pair("wizard-stack-coder")


def test_legacy_secret_attestation_writes_update_intent_before_client_mutation(
    tmp_path: Path,
) -> None:
    record = _task1_hermes_record()

    class ObservingCoderSecrets(FakeCoderSecrets):
        def __init__(
            self, *, receipt_path: Path, secrets: dict[str, CoderSecretMetadata]
        ) -> None:
            super().__init__(secrets=secrets)
            self.receipt_path = receipt_path
            self.observed_statuses: list[str] = []

        def write_secret(self, operation: str, spec: CoderSecretSpec) -> str:
            payload = json.loads(self.receipt_path.read_text(encoding="utf-8"))
            self.observed_statuses.append(payload["status"])
            assert payload["steps"][0]["operation"] == "update"
            assert payload["steps"][0]["status"] == "intent"
            assert self.writes == []
            return super().write_secret(operation, spec)

    spec = CoderSecretSpec(
        name=record["secret_name"],
        env_name=record["env_name"],
        value="openai",
        description=record["description"],
    )
    client = ObservingCoderSecrets(
        secrets={
            spec.name: CoderSecretMetadata(
                secret_id=record["secret_id"],
                name=spec.name,
                env_name=spec.env_name,
                description=spec.description,
            )
        },
        receipt_path=tmp_path / "coder-secret-receipts-v1.json",
    )
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="a" * 64)

    receipt = reconciler.reconcile((spec,))

    assert client.observed_statuses == ["running"]
    assert client.writes == [("update", spec.name)]
    assert receipt.steps[0].operation == "update"
    assert receipt.steps[0].status == "verified"


def test_unowned_secret_collision_rejects_altered_hermes_metadata_without_writes(
    tmp_path: Path,
) -> None:
    record = _task1_hermes_record()
    spec = CoderSecretSpec(
        name=record["secret_name"],
        env_name=record["env_name"],
        value="openai",
        description=record["description"],
    )
    client = FakeCoderSecrets(
        secrets={
            spec.name: CoderSecretMetadata(
                secret_id="00000000-0000-0000-0000-000000000001",
                name=spec.name,
                env_name=spec.env_name,
                description=spec.description,
            )
        }
    )
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="b" * 64)

    with pytest.raises(CoderSecretError, match="ownership"):
        reconciler.reconcile((spec,))

    assert client.writes == []


def test_secret_metadata_race_blocks_before_coder_mutation(tmp_path: Path) -> None:
    spec = CoderSecretSpec(
        name="hermes-inference-provider",
        env_name="HERMES_INFERENCE_PROVIDER",
        value="openai",
        description="Hermes provider for wizard-managed workspaces.",
    )

    class RacedMetadata(FakeCoderSecrets):
        calls = 0

        def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
            self.calls += 1
            if self.calls < 3:
                return ()
            return (
                CoderSecretMetadata(
                    secret_id="external-secret",
                    name=spec.name,
                    env_name=spec.env_name,
                    description=spec.description,
                ),
            )

    client = RacedMetadata()
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="c" * 64)

    with pytest.raises(CoderSecretError, match="metadata drift"):
        reconciler.reconcile((spec,))

    assert client.writes == []


def test_secret_update_crash_blocks_ambiguous_intent_without_replay(tmp_path: Path) -> None:
    spec = CoderSecretSpec(
        name="hermes-inference-provider",
        env_name="HERMES_INFERENCE_PROVIDER",
        value="openai",
        description="Hermes provider for wizard-managed workspaces.",
    )
    metadata = CoderSecretMetadata(
        secret_id="owned-secret",
        name=spec.name,
        env_name=spec.env_name,
        description=spec.description,
    )
    metadata_sha = metadata_sha256(metadata.to_dict())
    intent = CoderSecretReceiptStep(
        secret_name=spec.name,
        secret_id=metadata.secret_id,
        env_name=spec.env_name,
        description=spec.description,
        operation="update",
        status="intent",
        pre_metadata_sha256=metadata_sha,
        second_pre_metadata_sha256=metadata_sha,
        source_value_sha256=sha256(spec.value.encode()).hexdigest(),
        expected_post_sha256=metadata_sha,
        response_sha256=None,
        workspace_verification_sha256=None,
        updated_at="2026-07-30T00:00:00Z",
    )
    CoderSecretReceiptStore(tmp_path).write(
        CoderSecretReceipt(owner_id="c" * 64, status="running", steps=(intent,))
    )
    client = FakeCoderSecrets(secrets={spec.name: metadata}, values={spec.name: spec.value})
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="c" * 64)

    with pytest.raises(CoderSecretError, match="ambiguous"):
        reconciler.reconcile((spec,))

    assert client.writes == []


def test_task1_coder_secret_lock_json_loads_via_package_resources() -> None:
    payload = _task1_coder_secret_lock_payload()

    assert payload["schema_version"] == 1
    assert payload["clean_before_namespace_absent"] is True
    assert len(payload["records"]) == 4
    assert any(
        record["secret_name"] == "hermes-inference-provider"
        and record["env_name"] == "HERMES_INFERENCE_PROVIDER"
        and record["description"] == "Hermes provider for wizard-managed workspaces."
        for record in payload["records"]
    )


def test_dokploy_coder_health_accepts_immediate_public_success(monkeypatch) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(coder_module, "_public_https_health_check", lambda url: True)
    wait_calls: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_wait_for_public_https_health",
        lambda url: wait_calls.append(url) or False,
    )

    ok = backend.check_health(
        service=CoderResourceRecord("coder-service-1", "wizard-stack-coder"),
        url="https://coder.example.com/healthz",
    )

    assert ok is True
    assert wait_calls == []


def test_dokploy_coder_health_waits_for_public_route_on_first_apply(monkeypatch) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    backend._created_in_process = True
    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(coder_module, "_public_https_health_check", lambda url: False)
    waited_urls: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_wait_for_public_https_health",
        lambda url: waited_urls.append(url) or True,
    )

    ok = backend.check_health(
        service=CoderResourceRecord("coder-service-1", "wizard-stack-coder"),
        url="https://coder.example.com/healthz",
    )

    assert ok is True
    assert waited_urls == ["https://coder.example.com/healthz"]


def test_dokploy_coder_health_fails_closed_without_first_apply_warmup(monkeypatch) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(coder_module, "_public_https_health_check", lambda url: False)
    wait_calls: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_wait_for_public_https_health",
        lambda url: wait_calls.append(url) or True,
    )

    ok = backend.check_health(
        service=CoderResourceRecord("coder-service-1", "wizard-stack-coder"),
        url="https://coder.example.com/healthz",
    )

    assert ok is False
    assert wait_calls == []


def test_wait_for_public_https_health_uses_expanded_bounded_budget(monkeypatch) -> None:
    attempts: list[str] = []
    sleep_calls: list[float] = []

    def fake_public_https_health_check(url: str) -> bool:
        attempts.append(url)
        return False

    monkeypatch.setattr(coder_module, "_public_https_health_check", fake_public_https_health_check)
    monkeypatch.setattr(coder_module.time, "sleep", lambda delay: sleep_calls.append(delay))

    ok = coder_module._wait_for_public_https_health("https://coder.example.com/healthz")

    assert ok is False
    assert attempts == ["https://coder.example.com/healthz"] * 19
    assert sleep_calls == [5.0] * 18


def test_create_coder_first_user_retries_route_not_ready_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    sleep_calls: list[float] = []
    failures_remaining = 2

    def fake_coder_request(**kwargs: object) -> dict[str, object]:
        nonlocal failures_remaining
        requests.append(kwargs)
        if failures_remaining > 0:
            failures_remaining -= 1
            raise coder_module._CoderHTTPError(status=404)
        return {}

    monkeypatch.setattr(coder_module, "_coder_request", fake_coder_request)
    monkeypatch.setattr(coder_module.time, "sleep", lambda delay: sleep_calls.append(delay))

    coder_module._create_coder_first_user(
        hostname="coder.example.com",
        email="admin@example.com",
        password="ChangeMeSoon",
        attempts=3,
        delay_seconds=0.25,
    )

    assert [request["method"] for request in requests] == ["POST", "POST", "POST"]
    assert [request["path"] for request in requests] == [
        "/api/v2/users/first",
        "/api/v2/users/first",
        "/api/v2/users/first",
    ]
    assert sleep_calls == [0.25, 0.25]
    assert requests[-1]["payload"] == {
        "email": "admin@example.com",
        "username": "admin",
        "name": "Admin",
        "password": "ChangeMeSoon",
    }


def test_create_coder_first_user_persistent_404_raises_redacted_coder_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    def fake_coder_request(**kwargs: object) -> dict[str, object]:
        requests.append(kwargs)
        raise coder_module._CoderHTTPError(status=404)

    monkeypatch.setattr(coder_module, "_coder_request", fake_coder_request)
    monkeypatch.setattr(coder_module.time, "sleep", lambda delay: sleep_calls.append(delay))

    with pytest.raises(coder_module.CoderError) as exc_info:
        coder_module._create_coder_first_user(
            hostname="coder.example.com",
            email="admin@example.com",
            password="ChangeMeSoon",
            attempts=3,
            delay_seconds=0.25,
        )

    message = str(exc_info.value)
    assert "POST /api/v2/users/first" in message
    assert "HTTP 404" in message
    assert "ChangeMeSoon" not in message
    assert len(requests) == 3
    assert sleep_calls == [0.25, 0.25]


def test_default_workspace_name_uses_domain_derived_coder_safe_pattern() -> None:
    assert (
        coder_module._default_workspace_name("coder.yourwebsite.com", today=date(2026, 4, 18))
        == "yourwebsite-workspace-2026-04-18"
    )
    assert (
        coder_module._default_workspace_name("coder.openmerge.me", today=date(2026, 4, 18))
        == "openmergeme-workspace-2026-04-18"
    )


def test_ensure_default_workspace_creates_missing_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: ("existing-workspace",),
    )
    created: list[tuple[str, str, str, str, str]] = []
    monkeypatch.setattr(
        coder_module,
        "_create_default_workspace",
        lambda *,
        container_name,
        hostname,
        session_token,
        workspace_name,
        template_name: created.append(
            (container_name, hostname, session_token, workspace_name, template_name)
        ),
    )

    created_workspace = coder_module._ensure_default_workspace(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace-2026-04-18",
        template_name="ubuntu-vscode",
    )

    assert created_workspace is True
    assert created == [
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            "examplecom-workspace-2026-04-18",
            "ubuntu-vscode",
        )
    ]


def test_ensure_default_workspace_skips_existing_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: ("examplecom-workspace-2026-04-18",),
    )
    create_calls: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_create_default_workspace",
        lambda **kwargs: create_calls.append("called"),
    )

    created_workspace = coder_module._ensure_default_workspace(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace-2026-04-18",
        template_name="ubuntu-vscode",
    )

    assert created_workspace is False
    assert create_calls == []


def test_ensure_application_ready_bootstraps_first_user_with_shared_admin_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="clayton@openmerge.me",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_coder",
            user_name="wizard_stack_coder",
            password_secret_ref="wizard-stack-coder-postgres-password",
        ),
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    first_user_calls: list[tuple[str, str, str]] = []
    login_calls: list[tuple[str, str, str]] = []
    template_copy_calls: list[tuple[str, str, str]] = []
    template_replacements_by_name: dict[str, dict[str, str] | None] = {}
    template_push_calls: list[tuple[str, str, str, str]] = []
    ensure_workspace_calls: list[tuple[str, str, str, str, str]] = []
    secret_sync_calls: list[tuple[str, str, str, str | None]] = []
    migration_calls: list[ProductionMigrationInputs] = []
    _patch_workspace_runtime_image_replacements(monkeypatch, backend)

    def capture_migration(inputs: ProductionMigrationInputs) -> None:
        migration_calls.append(inputs)
        for source in inputs.sources:
            template_copy_calls.append(
                (inputs.container_name, str(source.directory), source.name)
            )
            template_replacements_by_name[source.name] = (
                _without_runtime_image_replacements(dict(source.replacements))
            )
            template_push_calls.append(
                (inputs.container_name, inputs.hostname, inputs.session_token, source.name)
            )

    monkeypatch.setattr(coder_module, "execute_template_migration", capture_migration)

    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: False)
    monkeypatch.setattr(
        coder_module,
        "_create_coder_first_user",
        lambda *, hostname, email, password: first_user_calls.append((hostname, email, password)),
    )
    monkeypatch.setattr(
        coder_module,
        "_coder_login",
        lambda *, hostname, email, password: login_calls.append((hostname, email, password))
        or "session-123",
    )
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: "wizard-stack-coder-container",
    )
    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_template_version_names", lambda **kwargs: ())
    monkeypatch.setattr(
        coder_module,
        "_sync_hermes_workspace_secrets",
        lambda **kwargs: secret_sync_calls.append(
            (
                str(kwargs["container_name"]),
                str(kwargs["hermes_inference_provider"]),
                str(kwargs["hermes_model"]),
                kwargs["ai_default_api_key"],
            )
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda *,
            container_name,
            template_dir,
            template_name,
            replacements: template_copy_calls.append((container_name, str(template_dir), template_name))
            or template_replacements_by_name.setdefault(
                template_name, _without_runtime_image_replacements(replacements)
            ),
    )
    monkeypatch.setattr(
        coder_module,
        "_push_default_template",
        lambda *,
        container_name,
        hostname,
        session_token,
        template_name,
        template_version_name=None: template_push_calls.append(
            (container_name, hostname, session_token, template_name)
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda *,
        container_name,
        hostname,
        session_token,
        workspace_name,
        template_name: ensure_workspace_calls.append(
            (container_name, hostname, session_token, workspace_name, template_name)
        )
        or True,
    )
    monkeypatch.setattr(
        coder_module,
        "_default_workspace_name",
        lambda hostname: "openmergeme-workspace-2026-04-18",
    )

    notes = backend.ensure_application_ready()

    assert first_user_calls == [("coder.example.com", "clayton@openmerge.me", "ChangeMeSoon")]
    assert login_calls == [("coder.example.com", "clayton@openmerge.me", "ChangeMeSoon")]
    assert len(migration_calls) == 1
    migration_sources = migration_calls[0].sources
    assert tuple(source.name for source in migration_sources) == (
        coder_module._required_template_names()
    )
    assert template_copy_calls == [
        ("wizard-stack-coder-container", str(source.directory), source.name)
        for source in migration_sources
    ]
    assert template_push_calls == [
        ("wizard-stack-coder-container", "coder.example.com", "session-123", source.name)
        for source in migration_sources
    ]
    assert template_replacements_by_name[coder_module._default_kdense_byok_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4-flash",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__": "$${LITELLM_VIRTUAL_KEY_CODER_KDENSE}",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_opencode_web_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_hermes_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__": "dokploy-litellm",
        "__DOKPLOY_WIZARD_HERMES_MODEL__": "opencode-go/deepseek-v4-flash",
        "__DOKPLOY_WIZARD_HERMES_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert ensure_workspace_calls == [
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            "openmergeme-workspace-2026-04-18",
            coder_module._default_template_name(),
        )
    ]
    assert secret_sync_calls == []
    assert notes == (
        "Provisioned initial Coder admin for 'clayton@openmerge.me'.",
        "Created default Coder workspace 'openmergeme-workspace-2026-04-18' for 'clayton@openmerge.me'.",
    )


def _migration_api_with_renamed_primary() -> MigrationApiFake:
    api = MigrationApiFake()
    api.rename_template(str(MIGRATION_PRIMARY_ID), "ubuntu-vscode-opencode-pi")
    api.rename_calls = 0
    return api


def _migration_pusher_at_targets(api: MigrationApiFake) -> MigrationPusherFake:
    pusher = MigrationPusherFake(api)
    for target in migration_targets():
        pusher.active_versions[target.name] = target.version_name
        pusher.versions[target.name] = (target.version_name,)
    return pusher


def test_template_migration_happy_preserves_primary_uuid_and_exact_four_names(
    tmp_path: Path,
) -> None:
    api = MigrationApiFake()
    pusher = MigrationPusherFake(api)

    receipt = migration_runner(tmp_path, api, pusher).run(
        str(MIGRATION_ORGANIZATION_ID), migration_targets()
    )

    assert receipt.status == "completed"
    assert {template.name for template in api.templates} == {
        target.name for target in migration_targets()
    }
    primary = next(
        template for template in api.templates if template.name == "ubuntu-vscode-opencode-pi"
    )
    assert primary.id == MIGRATION_PRIMARY_ID


def test_rename_intent_recovery_preserves_primary_uuid(tmp_path: Path) -> None:
    api = MigrationApiFake()
    pusher = MigrationPusherFake(api)

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce("after_response")).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    receipt = migration_runner(tmp_path, api, pusher).run(
        str(MIGRATION_ORGANIZATION_ID), migration_targets()
    )

    assert receipt.status == "completed"
    assert api.rename_calls == 1
    primary = next(
        template for template in api.templates if template.name == "ubuntu-vscode-opencode-pi"
    )
    assert primary.id == MIGRATION_PRIMARY_ID


def test_existing_push_intent_recovery_accepts_exact_intended_digest(
    tmp_path: Path,
) -> None:
    api = _migration_api_with_renamed_primary()
    pusher = MigrationPusherFake(api)

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce("after_response")).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    receipt = migration_runner(tmp_path, api, pusher).run(
        str(MIGRATION_ORGANIZATION_ID), migration_targets()
    )

    assert receipt.status == "completed"
    assert pusher.calls.count("ubuntu-vscode-opencode-pi") == 1


def test_absent_push_intent_recovery_accepts_exact_intended_digest(
    tmp_path: Path,
) -> None:
    api = MigrationApiFake()
    api.templates = [
        template for template in api.templates if template.id != MIGRATION_PRIMARY_ID
    ]
    pusher = MigrationPusherFake(api)
    del pusher.active_versions["ubuntu-vscode"]
    del pusher.versions["ubuntu-vscode"]

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce("after_response")).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    receipt = migration_runner(tmp_path, api, pusher).run(
        str(MIGRATION_ORGANIZATION_ID), migration_targets()
    )

    assert receipt.status == "completed"
    assert pusher.calls.count("ubuntu-vscode-opencode-pi") == 1


def test_delete_intent_recovery_template_delete_crash_completes(
    tmp_path: Path,
) -> None:
    api = _migration_api_with_renamed_primary()
    api.workspaces = []
    pusher = _migration_pusher_at_targets(api)

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce("after_response")).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    receipt = migration_runner(tmp_path, api, pusher).run(
        str(MIGRATION_ORGANIZATION_ID), migration_targets()
    )

    assert receipt.status == "completed"
    assert api.delete_template_calls == 2


@pytest.mark.parametrize(
    "crash_point",
    ["before_journal", "before_checkpoint", "before_request", "after_response"],
)
def test_migration_crash_matrix_resumes_from_each_journal_boundary(
    tmp_path: Path,
    crash_point: str,
) -> None:
    api = _migration_api_with_renamed_primary()
    pusher = MigrationPusherFake(api)

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce(crash_point)).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    receipt = migration_runner(tmp_path, api, pusher).run(
        str(MIGRATION_ORGANIZATION_ID), migration_targets()
    )

    assert receipt.status == "completed"


@pytest.mark.parametrize("version_mode", ["zero", "multiple"])
def test_absent_push_zero_or_multiple_intended_digest_matches_block(
    tmp_path: Path,
    version_mode: str,
) -> None:
    api = MigrationApiFake()
    api.templates = [
        template for template in api.templates if template.id != MIGRATION_PRIMARY_ID
    ]
    pusher = MigrationPusherFake(api)
    del pusher.active_versions["ubuntu-vscode"]
    del pusher.versions["ubuntu-vscode"]
    target = migration_targets()[0]

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce("after_response")).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    if version_mode == "zero":
        pusher.active_versions[target.name] = "unexpected-version"
        pusher.versions[target.name] = ("unexpected-version",)
    else:
        pusher.versions[target.name] = (target.version_name, target.version_name)

    with pytest.raises(RuntimeError, match="intended|ambiguous"):
        migration_runner(tmp_path, api, pusher).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )


def test_push_digest_mismatch_blocks_receipt_resume(tmp_path: Path) -> None:
    api = _migration_api_with_renamed_primary()
    pusher = MigrationPusherFake(api)

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce("after_response")).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    changed_targets = (
        replace(migration_targets()[0], rendered_sha256="e" * 64),
        *migration_targets()[1:],
    )

    with pytest.raises(RuntimeError, match="targets do not match"):
        migration_runner(tmp_path, api, pusher).run(
            str(MIGRATION_ORGANIZATION_ID), changed_targets
        )


def test_intervening_build_blocks_workspace_delete_resume(tmp_path: Path) -> None:
    api = _migration_api_with_renamed_primary()
    pusher = _migration_pusher_at_targets(api)

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migration_runner(tmp_path, api, pusher, MigrationCrashOnce("before_request")).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    intervening = migration_build(
        CoderId("cccccccc-cccc-cccc-cccc-cccccccccccc"), 2, "start", "running"
    )
    api.builds[MIGRATION_WORKSPACE_ID].append(intervening)
    api.workspaces[0] = replace(api.workspaces[0], latest_build=intervening)

    with pytest.raises(RuntimeError, match="delete recovery|exactly stopped"):
        migration_runner(tmp_path, api, pusher).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )


def test_migration_toctou_rename_ambiguous_collision_blocks_before_mutation(
    tmp_path: Path,
) -> None:
    api = MigrationApiFake()
    api.templates.append(
        replace(
            api.templates[0],
            id=CoderId("dddddddd-dddd-dddd-dddd-dddddddddddd"),
            name="ubuntu-vscode-opencode-pi",
        )
    )
    pusher = MigrationPusherFake(api)

    with pytest.raises(RuntimeError, match="collide"):
        migration_runner(tmp_path, api, pusher).run(
            str(MIGRATION_ORGANIZATION_ID), migration_targets()
        )
    assert api.rename_calls == 0


def test_coder_image_pin_rejects_nonaccepted_digest() -> None:
    wrong_digest = "ghcr.io/coder/coder@sha256:" + "1" * 64

    with pytest.raises(ValueError, match="accepted digest-pinned image"):
        coder_module.resolve_runtime_images({"CODER_IMAGE": wrong_digest})


def _write_coder_hash_checkpoint(
    state_dir: Path, *, service_name: str, compose_file: object
) -> None:
    rendered_compose = getattr(compose_file, "compose_file", compose_file)
    env_specs = getattr(compose_file, "env_specs", ())
    assert isinstance(rendered_compose, str)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="fingerprint",
            completed_steps=("coder",),
            compose_artifact_hashes={
                service_name: ComposeArtifactHashState.from_rendered_compose(
                    service_id=service_name,
                    rendered_compose=rendered_compose,
                    env_specs=env_specs,
                )
            },
        ),
    )
