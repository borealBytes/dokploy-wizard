# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import pytest

import dokploy_wizard.dokploy.coder as coder_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.coder import DokployCoderApi, DokployCoderBackend, _render_compose_file
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


def _expected_coder_fallback_models_json() -> str:
    return coder_module._litellm_workspace_fallback_models_json(
        default_alias="opencode-go/deepseek-v4.1-flash"
    )


def _expected_coder_fallback_models_json_escaped() -> str:
    return coder_module._shell_double_quote_escape(_expected_coder_fallback_models_json())


def test_coder_litellm_fallback_models_json_is_minimal_default_alias() -> None:
    aliases = json.loads(_expected_coder_fallback_models_json())

    # The workspace fetches the full live model set from /v1/models at boot; the
    # baked fallback is only the always-present default alias route. Stale
    # hardcoded samples (e.g. retired openrouter free slugs) must not reappear.
    assert aliases == ["opencode-go/deepseek-v4.1-flash"]
    assert "deepseek-v4.1-flash" not in aliases
    assert "minimax/minimax-m2.5:free" not in aliases
    assert "opencode-go/minimax-m2.7" not in aliases
    assert "opencode-go/*" not in aliases


def test_consolidated_default_template_is_the_workspace_default() -> None:
    # The default is the Hermes Desktop clone: the same consolidated surface (VS Code,
    # OpenCode, Pi, the community WebUI) plus the official webapp tile. The predecessor
    # is still pushed, because workspaces built from it must keep receiving versions
    # rather than freezing on the one they were created with.
    assert coder_module._default_template_name() == "ubuntu-vscode-hermes-desktop"
    assert coder_module._default_template_dir().name == "default-ubuntu-vscode-hermes-desktop"
    assert coder_module._default_template_dir().is_dir()
    assert coder_module._opencode_hermes_pi_template_name() in coder_module._required_template_names()
    assert coder_module._opencode_hermes_pi_template_dir().is_dir()
    assert coder_module._base_template_name() == "ubuntu-vscode"
    assert "ubuntu-vscode" in coder_module._required_template_names()


def test_consolidated_template_apps_are_desktop_server_opencode_hermes_terminal() -> None:
    template = Path(
        "templates/coder/default-ubuntu-vscode-opencode-hermes-pi/main.tf"
    ).read_text(encoding="utf-8")

    # VS Code browser + terminal + desktop come from the official code-server module.
    assert 'source   = "registry.coder.com/coder/code-server/coder"' in template
    assert 'slug         = "opencode"' in template
    assert 'slug         = "hermes-webui"' in template
    # Classic webui only: no ekko webui, no hermes dashboard surface anywhere.
    assert 'slug         = "hermes-web-ui"' not in template
    assert 'slug         = "hermes-dashboard"' not in template
    assert "npm install -g hermes-web-ui" not in template
    assert "hermes dashboard" not in template


def test_consolidated_template_serves_opencode_web_and_classic_hermes_with_cors_origin() -> None:
    template = Path(
        "templates/coder/default-ubuntu-vscode-opencode-hermes-pi/main.tf"
    ).read_text(encoding="utf-8")

    assert 'nohup opencode serve --hostname 127.0.0.1 --port "$OPENCODE_WEB_PORT"' in template
    # A stable serve password (seeded from the stack admin password) keeps
    # browser connections alive across rebuilds; serve otherwise randomizes
    # the password every boot.
    assert "OPENCODE_SERVER_PASSWORD=\"$(cat \"$OPENCODE_SERVER_PW_FILE\")\"" in template
    assert "/home/coder/.local/share/dokploy/opencode-server-password" in template
    assert "__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__" in template
    # opencode serve v2 has no base-path support: the app must ride the coder
    # wildcard host at an origin root, never behind a mounted-path proxy.
    assert "subdomain    = true" in template
    assert "/tmp/opencode-mounted-proxy.mjs" not in template
    assert "OPENCODE_PROXY_PORT" not in template
    # The CSRF gate must trust the public Coder origin or POSTs 403 silently.
    assert 'export HERMES_PUBLIC_ORIGIN="__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__"' in template
    assert 'HERMES_WEBUI_ALLOWED_ORIGINS="$HERMES_PUBLIC_ORIGIN"' in template
    assert "HERMES_WEBUI_TRUST_FORWARDED_HOST=1" in template


def test_consolidated_template_picker_advertises_per_model_costs() -> None:
    template = Path(
        "templates/coder/default-ubuntu-vscode-opencode-hermes-pi/main.tf"
    ).read_text(encoding="utf-8")

    # Pickers price models from models.dev (dollars per million tokens, the
    # unit opencode's UI bills in), mapping gateway aliases back to registry
    # entries through their sync/provider prefix; ":free" aliases are always
    # priced at zero and shown as "(free)".
    assert 'MODELS_DEV_URL = "https://models.dev/api.json"' in template
    assert '"sync/nvidia/": "nvidia",' in template
    assert '"sync/openrouter/": "openrouter",' in template
    assert '"opencode-go/": "opencode-go",' in template
    assert 'if alias.endswith(":free"):' in template
    # "$${" is Terraform's escape for a literal "${": this script sits in a
    # TF heredoc that would otherwise interpolate the f-string braces.
    assert "(in:$${_fmt_price(cost['input'])}/out:$${_fmt_price(cost['output'])})" in template
    assert '"models": _priced_models(),' in template
    assert '"models": [_pi_model_entry(model_id) for model_id in model_ids],' in template
    # models.dev (Cloudflare) rejects the default Python-urllib agent, so the
    # refresher must identify itself, and the registry must be cached daily
    # so the daemon's twice-daily pass does not refetch the whole catalog.
    assert '"User-Agent": "dokploy-wizard/model-refresh"' in template
    assert "MODELS_DEV_CACHE" in template
    # Steady-state picker refresh is twice-daily; failed refreshes retry on a
    # 5-minute heal-loop instead of idling until the next cadence pass.
    assert "time.sleep(43200)" in template
    assert "time.sleep(300 if refresh_rc else 43200)" in template
    assert "raise SystemExit(0 if fetched_model_ids else 3)" in template


def test_consolidated_template_refresher_avoids_terraform_interpolations() -> None:
    template = Path(
        "templates/coder/default-ubuntu-vscode-opencode-hermes-pi/main.tf"
    ).read_text(encoding="utf-8")

    match = re.search(
        r"cat > /home/coder/\.local/share/dokploy/model-refresh\.py <<'PY'\n(.*?)\nPY\n",
        template,
        re.S,
    )
    assert match is not None
    # Terraform interpolates dollar-brace expressions inside the surrounding
    # heredoc even when the inner shell heredoc is quoted, so every literal
    # interpolation in the script must use the double-dollar escape or
    # 'coder templates push' dies at terraform init.
    script = match.group(1).replace("$${", "")
    assert "${" not in script
    assert "%{" not in script
    # The model refresher daemon must also be (re)started by the workspace
    # bootstrap supervisor, with a visible log: envd-killed background children
    # silently dropped the boot-only daemon on the consolidated template.
    bootstrap_section = template.split("HERMES_BOOTSTRAP_SCRIPT=", 1)[1]
    assert "model-refresh.py --daemon >>/tmp/model-refresh-daemon.log" in bootstrap_section


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
    assert "opencode-go/deepseek-v4.1-flash" in custom_models
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


_TEMPLATE_DIRS_WITH_PI_AND_OPENCODE = (
    "default-ubuntu-code-server",
    "default-ubuntu-code-server-opencode-web",
    "default-ubuntu-code-server-kdense-byok",
    "default-ubuntu-code-server-hermes",
    "default-ubuntu-vscode-opencode-hermes-pi",
    "default-ubuntu-vscode-hermes-desktop",
)


@pytest.mark.parametrize("template_dir", _TEMPLATE_DIRS_WITH_PI_AND_OPENCODE)
def test_every_template_installs_pi_and_opencode_and_preseeds_litellm(template_dir: str) -> None:
    template = Path(f"templates/coder/{template_dir}/main.tf").read_text(encoding="utf-8")

    assert "curl -fsSL https://opencode.ai/v2/install" in template
    assert "pnpm add -g @earendil-works/pi-coding-agent" in template
    assert "command -v pi" in template
    assert "pi --version" in template
    assert 'Path("/home/coder/.config/opencode/opencode.json").write_text(' in template
    assert 'Path("/home/coder/.pi/agent/models.json").write_text(' in template
    assert '"npm": "@ai-sdk/openai-compatible"' in template
    assert '"api": "openai-completions"' in template
    assert "DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON" in template
    # Every template persists a self-contained refresher and a background daemon
    # so the model picker tracks the live gateway, not just boot time.
    assert "cat > /home/coder/.config/dokploy/model-refresh.env <<ENVEOF" in template
    assert "cat > /home/coder/.local/share/dokploy/model-refresh.py <<'PY'" in template
    assert "def setting(name: str, default: str" in template
    assert 'if "--daemon" in sys.argv[1:]' in template
    assert "model-refresh.py --daemon" in template
    assert "def _restore_inner_quotes" in template
    # The fallback reader must reject valid-but-not-list JSON (a quote-mangled
    # payload parses as a bare string and used to leak single-char models).
    assert "def _parse_fallback_models" in template


def test_base_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert 'Path("/home/coder/.config/opencode/opencode.json").write_text(' in template
    assert 'Path("/home/coder/.pi/agent/models.json").write_text(' in template


def test_opencode_web_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-opencode-web/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert "OPENCODE_WEB_PORT=4096" in template
    assert "OPENCODE_PROXY_PORT=4097" in template


def test_hermes_copilot_byok_template_settings() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-hermes/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert 'export HERMES_TEMPLATE_API_KEY="__DOKPLOY_WIZARD_HERMES_API_KEY__"' in template
    assert 'upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"' in template


def test_kdense_copilot_byok_uses_central_gateway() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    _assert_template_preseeds_copilot_byok(template)
    assert 'export KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL="__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__"' in template
    assert 'export KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY="__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__"' in template
    assert 'export KDENSE_COPILOT_DEFAULT_ALIAS="__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__/__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__"' in template
    assert 'base_url = setting("DOKPLOY_LITELLM_BASE_URL").rstrip("/")' in template
    assert 'KDENSE_CENTRAL_LITELLM_API_KEY="$${KDENSE_OPENCODE_GO_API_KEY:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY}"' in template
    assert 'api_key = setting("DOKPLOY_LITELLM_API_KEY")' in template
    assert '"DOKPLOY_LITELLM_API_KEY": "KDENSE_CENTRAL_LITELLM_API_KEY"' in template
    assert 'KDENSE_LOCAL_LITELLM_BASE_URL="http://localhost:$KDENSE_LITELLM_PORT"' in template
    assert 'append_env OPENROUTER_API_KEY ' not in template
    assert 'append_env NVIDIA_API_KEY ' not in template
    assert 'append_env ANTHROPIC_API_KEY ' not in template


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


def test_default_coder_template_restores_workspace_bootstrap_tools() -> None:
    template = Path("templates/coder/default-ubuntu-code-server/main.tf").read_text(
        encoding="utf-8"
    )

    assert "apt-get install -y curl git ca-certificates wget btop" in template
    assert "if ! command -v opencode >/dev/null 2>&1; then" in template
    assert (
        "if ! OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/v2/install | bash; then"
        in template
    )
    assert "if [ ! -x /home/coder/.opencode/bin/opencode ]; then" in template
    assert 'echo "OpenCode installer did not produce a usable binary" >&2' in template
    assert "exit 1" in template
    assert "if [ -x /home/coder/.opencode/bin/opencode ]; then" in template
    assert "ln -sf /home/coder/.opencode/bin/opencode /usr/local/bin/opencode" in template
    assert "if ! command -v zellij >/dev/null 2>&1; then" in template
    assert "zellij-$${ARCH}-unknown-linux-musl.tar.gz" in template
    assert "if ! command -v node >/dev/null 2>&1; then" in template
    assert "curl -fsSL https://deb.nodesource.com/setup_22.x | $_SUDO -E bash -" in template
    assert "$_SUDO apt-get install -y nodejs" in template
    assert "$_SUDO corepack enable" in template
    assert "$_SUDO corepack prepare pnpm@10.27.0 --activate" in template
    assert "export PNPM_HOME=/home/coder/.local/share/pnpm" in template
    assert 'export PATH="$PNPM_HOME/bin:$PATH"' in template
    assert '/home/coder/.bashrc || echo "export PNPM_HOME=/home/coder/.local/share/pnpm" >> /home/coder/.bashrc' in template
    assert r'/home/coder/.bashrc || echo "export PATH=\"$PNPM_HOME/bin:$PATH\"" >> /home/coder/.bashrc' in template
    assert '/home/coder/.profile || echo "export PNPM_HOME=/home/coder/.local/share/pnpm" >> /home/coder/.profile' in template
    assert r'/home/coder/.profile || echo "export PATH=\"$PNPM_HOME/bin:$PATH\"" >> /home/coder/.profile' in template
    assert "pnpm add -g @earendil-works/pi-coding-agent" in template
    assert "command -v pi" in template
    assert "pi --version" in template
    assert "bash -lc 'command -v pi && pi --version'" in template
    assert 'export AI_DEFAULT_PROVIDER="$${AI_DEFAULT_PROVIDER:-__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__}"' in template
    assert 'export AI_DEFAULT_MODEL="$${AI_DEFAULT_MODEL:-__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__}"' in template
    assert 'export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__}"' in template
    assert 'export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__}"' in template
    assert 'export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$AI_DEFAULT_BASE_URL}"' in template
    assert 'export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$AI_DEFAULT_API_KEY}"' in template
    assert 'export LITELLM_DEFAULT_ALIAS="$AI_DEFAULT_PROVIDER/$AI_DEFAULT_MODEL"' in template
    assert 'export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"' in template
    assert 'timeout=15' in template
    assert 'for attempt in range(4):' in template
    assert 'payload = json.load(response)' in template
    assert 'except (OSError, ValueError, urllib.error.URLError):' in template
    assert 'payload = {"data": []}' in template
    assert 'model_id = item.get("id")' in template
    assert 'and not normalized.endswith("/*")' in template
    assert 'and not normalized.startswith("openai/")' in template
    assert 'model_ids = list(dict.fromkeys(fetched_model_ids + fallback_models))' in template
    assert '"npm": "@ai-sdk/openai-compatible"' in template
    assert '"options": {"baseURL": base_url, "apiKey": api_key}' in template
    assert '"models": {model_id: {} for model_id in model_ids}' in template
    assert 'Path("/home/coder/.config/opencode/opencode.json").write_text(' in template
    assert '"models": [{"id": model_id, "name": model_id} for model_id in model_ids]' in template
    assert 'Path("/home/coder/.pi/agent/models.json").write_text(' in template
    assert "pi.dev/install.sh" not in template
    assert 'resource "coder_app"' not in template
    assert "pi-web-ui" not in template
    assert "vite preview" not in template
    assert "subdomain =" not in template


def test_default_opencode_web_template_includes_web_app() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-opencode-web/main.tf").read_text(
        encoding="utf-8"
    )

    assert "apt-get install -y curl git ca-certificates wget btop" in template
    assert "if ! command -v opencode >/dev/null 2>&1; then" in template
    assert (
        "if ! OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/v2/install | bash; then"
        in template
    )
    assert "if [ ! -x /home/coder/.opencode/bin/opencode ]; then" in template
    assert "ln -sf /home/coder/.opencode/bin/opencode /usr/local/bin/opencode" in template
    assert "NEED_NODE=true" in template
    assert "Shared LiteLLM defaults keep OpenCode Web on the wizard-managed gateway." in template
    assert 'export AI_DEFAULT_PROVIDER="$${AI_DEFAULT_PROVIDER:-__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__}"' in template
    assert 'export AI_DEFAULT_MODEL="$${AI_DEFAULT_MODEL:-__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__}"' in template
    assert 'export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__}"' in template
    assert 'export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__}"' in template
    assert 'export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$AI_DEFAULT_BASE_URL}"' in template
    assert 'export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$AI_DEFAULT_API_KEY}"' in template
    assert 'export LITELLM_DEFAULT_ALIAS="$AI_DEFAULT_PROVIDER/$AI_DEFAULT_MODEL"' in template
    assert 'export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"' in template
    assert 'timeout=15' in template
    assert 'for attempt in range(4):' in template
    assert 'payload = json.load(response)' in template
    assert 'payload = {"data": []}' in template
    assert 'model_ids = list(dict.fromkeys(fetched_model_ids + fallback_models))' in template
    assert '"npm": "@ai-sdk/openai-compatible"' in template
    assert '"options": {"baseURL": base_url, "apiKey": api_key}' in template
    assert '"models": {model_id: {} for model_id in model_ids}' in template
    assert 'Path("/home/coder/.config/opencode/opencode.json").write_text(' in template
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
    assert (
        'icon         = "https://raw.githubusercontent.com/anomalyco/opencode/refs/heads/dev/packages/ui/src/assets/favicon/favicon-v3.svg"'
        in template
    )
    assert 'url          = "http://localhost:4097"' in template
    assert 'share        = "owner"' in template
    assert "subdomain    = false" in template
    assert 'url       = "http://localhost:4097"' in template


def test_required_template_names_cover_only_remaining_templates() -> None:
    assert coder_module._required_template_names() == (
        coder_module._default_template_name(),
        coder_module._opencode_hermes_pi_template_name(),
        coder_module._base_template_name(),
        coder_module._default_opencode_web_template_name(),
        coder_module._default_kdense_byok_template_name(),
        coder_module._default_hermes_template_name(),
    )
    names = coder_module._required_template_names()
    assert len(names) == 6
    # The default is now the template the desktop accessor names, so a list that
    # naively includes both would push the same template twice every run.
    assert len(set(names)) == len(names)


def test_retired_template_names_cover_removed_templates() -> None:
    assert coder_module._retired_template_names() == (
        "ubuntu-vscode-openwork",
        "ubuntu-vscode-pi-web",
    )
    assert set(coder_module._retired_template_names()).isdisjoint(
        coder_module._required_template_names()
    )


def test_stable_serve_password_self_heals_a_placeholder_left_by_an_older_build() -> None:
    # A replacement token that is never replaced does not fail a plan: the literal
    # string becomes the password, which is exactly what one released build shipped
    # (the stored file was 43 bytes, the length of the token). Workspaces keep that
    # file across rebuilds, so the block has to recognize and rewrite it.
    for name in (
        "default-ubuntu-vscode-hermes-desktop",
        "default-ubuntu-vscode-opencode-hermes-pi",
    ):
        template = Path(f"templates/coder/{name}/main.tf").read_text(encoding="utf-8")
        assert "grep -q '__DOKPLOY_WIZARD_' \"$OPENCODE_SERVER_PW_FILE\"" in template


def test_hermes_desktop_template_drives_commands_the_cli_actually_ships() -> None:
    # An earlier version of this block drove `hermes webapp` with `webapp.*` config
    # keys. Neither exists on the installed CLI (v0.22.5): it answered
    # "'webapp' is not a `hermes` command" and stored the keys with a warning that it
    # "may not read" them — which it does not. Everything asserted here was read off
    # the box: --help, the plugin's own error text, and the served endpoints.
    template = Path(
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
    ).read_text(encoding="utf-8")

    # The artifact is the Desktop renderer the gateway hosts in a browser, not the
    # dashboard bundle. These two dists are different builds of different apps, and
    # serving the wrong one is the exact regression this guard exists for: it looks
    # healthy, signs in fine, and renders the old UI.
    # Judged on executable lines only: the comments name the dashboard bundle to say
    # this is not it, and a guard that bans the words would ban the explanation.
    code = "\n".join(line for line in template.splitlines() if not line.lstrip().startswith("#"))
    assert "hermes webapp --build-only" in template
    assert "apps/desktop/dist-webapp" in template
    assert "hermes_cli/web_dist" not in code
    assert "npm run build" not in code

    # Turned on through the gateway's own config surface, which it reads only at start.
    assert "hermes config set webapp.enabled true" in template
    assert "hermes config set webapp.host 127.0.0.1" in template
    assert 'hermes config set webapp.port "$HERMES_WEBAPP_INTERNAL_PORT"' in template
    # public_url must stay EMPTY. This is the whole "no second sign-in" contract, and
    # it was paid for twice on the live box: upstream's webapp listener engages the
    # auth gate whenever webapp.public_url names a host (gateway/run_api.py — the
    # desktop-loopback exemption never runs for it), and the credential that gate
    # wants is the one-use link from `hermes webapp`, which a tile click cannot
    # chase. Both public-URL designs regressed the tile; this line is where they die.
    assert 'hermes config set webapp.public_url ""' in template
    assert 'webapp.public_url "$HERMES' not in template
    assert "hermes_wildcard_suffix" not in template
    # The tile port belongs to a stateless loopback front, because the webapp validates
    # Host (HTTP middleware) and Host+Origin (WebSocket boundary) against its loopback
    # bind while auth is off. The front's job is exactly those rewrites; anything with
    # credentials or cookies in it here means the sign-in is creeping back.
    assert 'export HERMES_WEBAPP_INTERNAL_PORT="$${HERMES_WEBAPP_INTERNAL_PORT:-9121}"' in template
    assert "hermes-webapp-front.mjs" in template
    assert 'server.on("upgrade"' in template
    assert "delete headers.origin;" in template
    assert "host: BACKEND_HOST" in template
    assert 'PROXY_PORT="$HERMES_WEBAPP_PORT"' in template
    assert "no sign-in of its own" in template
    assert "sign in as '" not in code
    # The HTTP 200 that proves the boundary is happy is NOT the app opening: the
    # Desktop bundle refuses to boot without its sessionStorage credential (the
    # WEBAPP_LAUNCH_REQUIRED card), which a curl-only check cannot see. The front
    # must hand the page the gateway's stored browser credential, and the listener's
    # own trust-proxy patch (when applied) is signalled by the marker file.
    assert 'hermes.webapp.session.v1' in template
    assert "sessionStorage.setItem" in template
    assert '"webapp", "session-token"' in template
    assert "$HERMES_HOME/webapp/trust-proxy" in template
    assert 'nohup env HERMES_HOME="$HERMES_HOME"' in template
    # The entry page ships a CSP that allowlists inline scripts by sha256, so an
    # injected <script> the header does not name is silently refused: the browser
    # then shows the sign-in card even though curl sees the grant in the HTML.
    # The front must extend script-src with its own hash.
    assert "content-security-policy" in template
    assert "createHash" in template and "sha256" in template
    assert "allowInCsp" in template

    # The old dashboard is gone from this path, not layered behind the new UI. The one
    # mention left is the comment that says it is not the dashboard; the auth env keys
    # keep the CLI's own HERMES_DASHBOARD_* names and stay.
    assert "hermes dashboard" not in code
    assert 'hermes dashboard --host' not in template
    assert "hermes dashboard --stop" not in template
    assert "hermes dashboard --status" not in template
    assert 'slug         = "hermes-dashboard"' not in template

    # Version floor: the command does not exist on the release channel, so the template
    # pins the channel at install time and repairs an older checkout before serving.
    assert "if ! hermes webapp --help >/dev/null 2>&1; then" in template
    assert 'hermes update --set-channel "$HERMES_DESKTOP_CHANNEL"' in template
    assert 'hermes update --channel "$HERMES_DESKTOP_CHANNEL"' in template
    assert '--channel "__DOKPLOY_WIZARD_HERMES_INSTALL_CHANNEL__"' in template
    # A build still without the artifact serves nothing rather than the old UI.
    assert "HERMES WEBAPP UNAVAILABLE" in template
    assert 'if [ "$HERMES_WEBAPP_CAPABLE" = "1" ]; then' in template

    # /health answers 302 -> /login once the sign-in gate is on, so both the tile
    # healthcheck and the watchdog have to use the endpoint that answers unsigned.
    assert 'HERMES_WEBAPP_HEALTH_PATH="/api/health"' in template
    # The sign-in name has to be guessable: the login page appears before any
    # workspace file is readable. It is the owner's Coder username, rewritten every
    # boot so volumes built when it was a fixed word follow the owner.
    assert 'export HERMES_WEBAPP_BASIC_AUTH_USERNAME="${local.username}"' in template
    assert 'upsert_env HERMES_DASHBOARD_BASIC_AUTH_USERNAME' in template
    assert 'upsert_env_if_unset HERMES_DASHBOARD_BASIC_AUTH_USERNAME' not in template
    assert 'url       = "http://localhost:9120/api/health"' in template
    assert "healthy_webapp()" in template
    assert "restart_gateway_for_webapp()" in template
    # The tile port is the front; the internal port is the listener. Judging only the
    # front would restart a healthy gateway when a dead front is the whole problem,
    # and judging only the listener would call the tile healthy with a dead front.
    assert "healthy_webapp_backend()" in template
    assert "start_webapp_front()" in template
    assert "WEBAPP_FRONT=/home/coder/.local/share/dokploy/hermes-webapp-front.mjs" in template
    assert "healthy_dashboard()" not in template
    assert "start_dashboard()" not in template
    assert '"http://127.0.0.1:\\$1/health"' in template  # the community WebUI keeps /health


def test_desktop_hermes_channel_is_defaulted_and_sanitised() -> None:
    # An operator who never heard of the channel still gets a working tile, and a typo
    # must not become a broken workspace boot minutes later at install time, far from
    # the env file that caused it.
    assert coder_module._DEFAULT_HERMES_DESKTOP_CHANNEL == "main"
    assert coder_module._normalize_hermes_channel("") == "main"
    assert coder_module._normalize_hermes_channel("  main  ") == "main"
    assert coder_module._normalize_hermes_channel("stable") == "stable"
    assert coder_module._normalize_hermes_channel("main; rm -rf /") == "main"
    assert coder_module._normalize_hermes_channel('main"') == "main"


def test_coder_app_open_in_values_are_ones_the_provider_accepts() -> None:
    # The provider validates open_in during template import, so a value it does not
    # know costs a remote deploy cycle to discover: terraform plan fails and the
    # template silently never arrives.
    accepted = {"", "window", "tab", "slim-window"}
    used_by_template: dict[str, list[str]] = {}
    for template_dir in sorted(Path("templates/coder").iterdir()):
        manifest = template_dir / "main.tf"
        if not manifest.is_file():
            continue
        used = re.findall(r'open_in\s*=\s*"([^"]*)"', manifest.read_text(encoding="utf-8"))
        if used:
            used_by_template[template_dir.name] = used

    offenders = {
        name: [value for value in values if value not in accepted]
        for name, values in used_by_template.items()
        if any(value not in accepted for value in values)
    }
    assert offenders == {}
    # Self-check: the scan has to see the tile this exists to protect.
    assert used_by_template["default-ubuntu-vscode-hermes-desktop"] == ["tab"]


def test_safe_progress_reason_keeps_the_terraform_diagnosis_when_clipping() -> None:
    # The CLI prints its banner and streaming log first and the actual reason last, so
    # clipping the head used to leave "exit status 1" and throw away the diagnosis.
    reason = "PLAN START " + ("streamed output " * 80) + "Error: Invalid value for variable: webapp.port must be an integer"

    clipped = coder_module._safe_progress_reason(reason, limit=200)

    assert len(clipped) <= 200
    assert "Invalid value for variable" in clipped
    assert "PLAN START" in clipped


def test_seeded_template_names_fit_coder_name_limit() -> None:
    # Coder rejects an over-long name only after packaging the template, and a failed
    # push for a non-default template is a note rather than a failed install. That
    # combination silently cost the Hermes Desktop template on a box that reported a
    # clean modify, so the limit is checked here and at push time.
    names = {
        coder_module._default_template_name(),
        coder_module._opencode_hermes_pi_template_name(),
        coder_module._base_template_name(),
        coder_module._default_opencode_web_template_name(),
        coder_module._default_kdense_byok_template_name(),
        coder_module._default_hermes_template_name(),
        *coder_module._retired_template_names(),
    }
    limit = coder_module._CODER_MAX_NAME_LENGTH
    assert {name for name in names if len(name) > limit} == set()
    # Template directories are named after their template, so a rename cannot drift.
    assert (
        coder_module._default_hermes_desktop_template_dir().name
        == f"default-{coder_module._default_hermes_desktop_template_name()}"
    )

    with pytest.raises(coder_module.CoderError, match="at most 32"):
        coder_module._ensure_template_name_length("ubuntu-vscode-opencode-hermes-desktop")


def test_retire_removed_templates_deletes_only_retired_templates(monkeypatch) -> None:
    delete_calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        del kwargs
        delete_calls.append(command)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    def fake_list_templates(*, container_name: str, hostname: str, session_token: str) -> tuple[dict[str, object], ...]:
        del container_name, hostname, session_token
        return (
            {"name": "ubuntu-vscode"},
            {"name": "ubuntu-vscode-openwork"},
            {"name": "operator-custom-template"},
        )

    monkeypatch.setattr(coder_module, "_list_templates", fake_list_templates)
    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    retired = coder_module._retire_removed_templates(
        container_name="stack-coder",
        hostname="coder.example.com",
        session_token="tok",
    )

    assert retired == ("ubuntu-vscode-openwork",)
    assert len(delete_calls) == 1
    assert delete_calls[0][-4:] == ["templates", "delete", "ubuntu-vscode-openwork", "--yes"]


def test_retire_removed_templates_reports_delete_failure(monkeypatch) -> None:
    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout="",
            stderr="template is used by 1 workspace",
        )

    def fake_list_templates(*, container_name: str, hostname: str, session_token: str) -> tuple[dict[str, object], ...]:
        del container_name, hostname, session_token
        return ({"name": "ubuntu-vscode-pi-web"},)

    monkeypatch.setattr(coder_module, "_list_templates", fake_list_templates)
    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    try:
        coder_module._retire_removed_templates(
            container_name="stack-coder",
            hostname="coder.example.com",
            session_token="tok",
        )
    except coder_module.CoderError as error:
        assert "ubuntu-vscode-pi-web" in str(error)
    else:
        raise AssertionError("Expected CoderError when template delete fails")


def test_retire_removed_templates_is_silent_when_nothing_retired(monkeypatch) -> None:
    def fake_list_templates(*, container_name: str, hostname: str, session_token: str) -> tuple[dict[str, object], ...]:
        del container_name, hostname, session_token
        return ({"name": "ubuntu-vscode"},)

    def fail_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"Unexpected subprocess call: {command}")

    monkeypatch.setattr(coder_module, "_list_templates", fake_list_templates)
    monkeypatch.setattr(coder_module.subprocess, "run", fail_run)

    assert (
        coder_module._retire_removed_templates(
            container_name="stack-coder",
            hostname="coder.example.com",
            session_token="tok",
        )
        == ()
    )


def test_readme_documents_coder_litellm_scope_boundaries() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "OpenCode Web inherits wizard-managed LiteLLM defaults" in readme
    assert "OpenWork" not in readme
    assert "Pi Web" not in readme


def test_readme_documents_pi_and_opencode_on_every_template() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "Every wizard-managed template installs the OpenCode CLI and the Pi coding agent" in readme
    assert "pre-wired on first boot" in readme


def test_default_kdense_byok_template_includes_upstream_parameterized_stack() -> None:
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
    assert "missing_packages=()" in template
    assert "for package in curl ca-certificates wget python3; do" in template
    assert "if ! command -v git >/dev/null 2>&1; then" in template
    assert "if ! command -v btop >/dev/null 2>&1; then" in template
    assert '$_SUDO apt-get install -y "$${missing_packages[@]}"' in template
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in template
    assert "https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt" in template
    assert "tar -xJf - -C /usr/local --strip-components=1 --no-same-owner" in template
    assert "NPM_CONFIG_PREFIX=/home/coder/.local npm install -g @google/gemini-cli" in template
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
    assert "sync_kdense_source() {" in template
    assert 'EXPECTED_REPO_URL="https://github.com/K-Dense-AI/k-dense-byok.git"' in template
    assert (
        'git clone --depth 1 --branch main https://github.com/K-Dense-AI/k-dense-byok.git "$KDENSE_SRC_DIR"'
        in template
    )
    assert (
        'curl -fsSL https://codeload.github.com/K-Dense-AI/k-dense-byok/tar.gz/refs/heads/main | tar -xz --strip-components=1 -C "$KDENSE_SRC_DIR"'
        in template
    )
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
    assert "KDENSE_REV=archive-main" in template
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
    assert '"id": "openai/deepseek-v4.1-flash"' not in template
    assert "KDENSE_SETUP_STAMP=/home/coder/.cache/kdense-byok-setup-rev" in template
    assert "KDENSE_SETUP_KEY=v11-central-litellm-only" in template
    assert (
        'KDENSE_SETUP_ID="$KDENSE_REV:$KDENSE_SETUP_KEY:$KDENSE_DEFAULT_MODEL_EFFECTIVE:$KDENSE_EXPERT_MODEL_EFFECTIVE"'
        in template
    )
    assert '[ ! -f "$KDENSE_SRC_DIR/web/.next/BUILD_ID" ]' in template
    assert "uv sync --python 3.13 --no-dev --quiet" in template
    assert "if [ -f web/package-lock.json ]; then" in template
    assert (
        "(cd web && NEXT_PUBLIC_ADK_API_URL= npm ci --silent && NEXT_PUBLIC_ADK_API_URL= npm run build)"
        in template
    )
    assert (
        "(cd web && NEXT_PUBLIC_ADK_API_URL= npm install --silent && NEXT_PUBLIC_ADK_API_URL= npm run build)"
        in template
    )
    assert 'printf \'%s\' "$KDENSE_SETUP_ID" > "$KDENSE_SETUP_STAMP"' in template
    assert "KDENSE_NEEDS_PREP=false" in template
    assert 'if [ ! -d "$KDENSE_SRC_DIR/sandbox/.gemini/skills" ]; then' in template
    assert "KDENSE_NEEDS_PREP=true" in template
    assert "uv run python prep_sandbox.py" in template
    assert ">/tmp/kdense-prep.log 2>&1 &" in template
    assert 'pkill -f "next dev --hostname 127.0.0.1 --port $KDENSE_UI_PORT"' in template
    assert 'pkill -f "next start --hostname 127.0.0.1 --port $KDENSE_UI_PORT"' in template
    assert (
        "NEXT_PUBLIC_ADK_API_URL= npm run start -- --hostname 127.0.0.1 --port $KDENSE_UI_PORT"
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
    assert (
        'icon         = "https://raw.githubusercontent.com/K-Dense-AI/k-dense-byok/main/web/public/brand/kdense-logo-dark.png"'
        in template
    )
    assert 'url          = "http://localhost:3001"' in template
    assert "subdomain    = true" in template
    assert 'url       = "http://localhost:3001/health"' in template


def test_kdense_calls_central_litellm() -> None:
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


def test_no_openrouter_wildcard_in_kdense_config() -> None:
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


def test_kdense_template_preserves_restored_byok_source_state() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-kdense-byok/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'data "coder_parameter" "kdense_opencode_go_base_url" {' in template
    assert 'display_name = "Central LiteLLM Base URL"' in template
    assert 'default      = "https://opencode.ai/zen/go/v1"' in template
    assert (
        'KDENSE_CENTRAL_LITELLM_BASE_URL="$${KDENSE_OPENCODE_GO_BASE_URL:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL}"'
        in template
    )
    assert "KDENSE_TEMPLATE_OPENCODE_GO_BASE_URL_PLACEHOLDER" not in template
    assert "KDENSE_TEMPLATE_OPENCODE_GO_API_KEY_PLACEHOLDER" not in template
    assert 'append_env OPENROUTER_API_KEY ' not in template
    assert 'append_env NVIDIA_API_KEY ' not in template
    assert 'append_env ANTHROPIC_API_KEY ' not in template


def test_default_hermes_template_includes_full_web_stack() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-hermes/main.tf").read_text(
        encoding="utf-8"
    )

    assert "$_SUDO apt-get install -y curl git ca-certificates wget btop python3" in template
    assert "curl -fsSL https://deb.nodesource.com/setup_24.x | $_SUDO -E bash -" in template
    assert (
        'HERMES_HOME="$HERMES_HOME" HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup'
        in template
    )
    assert "export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in template
    assert (
        'export HERMES_TEMPLATE_PROVIDER="__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__"' in template
    )
    assert 'export HERMES_TEMPLATE_MODEL="__DOKPLOY_WIZARD_HERMES_MODEL__"' in template
    assert 'export HERMES_TEMPLATE_BASE_URL="__DOKPLOY_WIZARD_HERMES_BASE_URL__"' in template
    assert 'export HERMES_TEMPLATE_API_KEY="__DOKPLOY_WIZARD_HERMES_API_KEY__"' in template
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
    assert (
        'icon         = "https://raw.githubusercontent.com/NousResearch/hermes-agent/refs/heads/main/acp_registry/icon.svg"'
        in template
    )
    assert 'url          = "http://localhost:9120"' in template
    assert 'resource "coder_app" "hermes_web_ui"' in template
    assert (
        'icon         = "https://raw.githubusercontent.com/EKKOLearnAI/hermes-web-ui/refs/heads/main/packages/client/public/favicon.svg"'
        in template
    )
    assert 'url          = "http://localhost:8649"' in template
    assert 'resource "coder_app" "hermes_webui"' in template
    assert (
        'icon         = "https://raw.githubusercontent.com/nesquena/hermes-webui/refs/heads/master/static/favicon.svg"'
        in template
    )
    assert 'url          = "http://localhost:8788"' in template
    assert "HERMIES_PROVIDER" not in template
    assert "HERMEIS_OPENCODE_GO_MODEL" not in template
    assert "HERMIES_BASE_USL" not in template
    assert "HERMIES_API_MODE" not in template


def test_hermes_template_uses_litellm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@openmerge.me",
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
    secret_sync_calls: list[dict[str, object]] = []

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
        replacements: template_replacements_by_name.setdefault(template_name, replacements),
    )
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)

    backend.ensure_application_ready()

    assert secret_sync_calls == [
        {
            "container_name": "wizard-stack-coder-container",
            "hostname": "coder.example.com",
            "session_token": "session-123",
            "hermes_inference_provider": "openai",
            "hermes_model": "local-model.internal/unsloth-active",
            "ai_default_base_url": "http://wizard-stack-shared-litellm:4000",
            "ai_default_api_key": "litellm-coder-hermes-key",
        }
    ]
    assert template_replacements_by_name[coder_module._default_hermes_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__": "openai",
        "__DOKPLOY_WIZARD_HERMES_MODEL__": "local-model.internal/unsloth-active",
        "__DOKPLOY_WIZARD_HERMES_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_HERMES_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }


def test_base_and_opencode_web_templates_receive_shared_litellm_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@openmerge.me",
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
        replacements: template_replacements_by_name.setdefault(template_name, replacements),
    )
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())

    backend.ensure_application_ready()

    consolidated_replacements = template_replacements_by_name[coder_module._default_template_name()]
    hermes_legacy_replacements = template_replacements_by_name[coder_module._default_hermes_template_name()]
    assert consolidated_replacements["__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__"] == "https://coder.example.com"
    assert consolidated_replacements["__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__"] == "ChangeMeSoon"
    assert consolidated_replacements["__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__"] == "wizard-stack-shared"
    assert consolidated_replacements["__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"] == (
        _expected_coder_fallback_models_json_escaped()
    )
    # The two templates have to agree on how Hermes reaches the gateway. The keys are
    # named rather than filtered by prefix because the default template now carries a
    # Hermes key the legacy one never had; a prefix filter turns that into a failure
    # with no explanation attached.
    hermes_wiring_keys = (
        "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__",
        "__DOKPLOY_WIZARD_HERMES_MODEL__",
        "__DOKPLOY_WIZARD_HERMES_BASE_URL__",
        "__DOKPLOY_WIZARD_HERMES_API_KEY__",
    )
    assert {key: consolidated_replacements[key] for key in hermes_wiring_keys} == {
        key: hermes_legacy_replacements[key] for key in hermes_wiring_keys
    }
    assert "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__" in consolidated_replacements
    assert "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__" not in hermes_legacy_replacements
    assert template_replacements_by_name[coder_module._base_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_opencode_web_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_kdense_byok_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__": "$${LITELLM_VIRTUAL_KEY_CODER_KDENSE}",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert "ubuntu-vscode-openwork" not in template_replacements_by_name
    assert "ubuntu-vscode-pi-web" not in template_replacements_by_name


def test_ensure_application_ready_reseeds_templates_for_healthy_existing_coder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@openmerge.me",
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
    template_push_calls: list[str] = []
    ensure_workspace_calls: list[object] = []

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
        replacements: template_replacements_by_name.setdefault(template_name, replacements),
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
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())

    notes = backend.ensure_application_ready()

    assert template_push_calls == [
        coder_module._default_template_name(),
        coder_module._opencode_hermes_pi_template_name(),
        coder_module._base_template_name(),
        coder_module._default_opencode_web_template_name(),
        coder_module._default_kdense_byok_template_name(),
        coder_module._default_hermes_template_name(),
    ]
    assert ensure_workspace_calls == []
    assert notes == (
        "Seeded default Coder template 'ubuntu-vscode-hermes-desktop'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-hermes-pi'.",
        "Seeded default Coder template 'ubuntu-vscode'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-web'.",
        "Seeded default Coder template 'ubuntu-vscode-kdense-byok'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes'.",
    )
    consolidated_replacements = template_replacements_by_name[coder_module._default_template_name()]
    hermes_legacy_replacements = template_replacements_by_name[coder_module._default_hermes_template_name()]
    assert consolidated_replacements["__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__"] == "https://coder.example.com"
    assert consolidated_replacements["__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__"] == "ChangeMeSoon"
    assert consolidated_replacements["__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__"] == "wizard-stack-shared"
    assert consolidated_replacements["__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"] == (
        _expected_coder_fallback_models_json_escaped()
    )
    # The two templates have to agree on how Hermes reaches the gateway. The keys are
    # named rather than filtered by prefix because the default template now carries a
    # Hermes key the legacy one never had; a prefix filter turns that into a failure
    # with no explanation attached.
    hermes_wiring_keys = (
        "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__",
        "__DOKPLOY_WIZARD_HERMES_MODEL__",
        "__DOKPLOY_WIZARD_HERMES_BASE_URL__",
        "__DOKPLOY_WIZARD_HERMES_API_KEY__",
    )
    assert {key: consolidated_replacements[key] for key in hermes_wiring_keys} == {
        key: hermes_legacy_replacements[key] for key in hermes_wiring_keys
    }
    assert "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__" in consolidated_replacements
    assert "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__" not in hermes_legacy_replacements
    assert template_replacements_by_name[coder_module._base_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_opencode_web_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "litellm-coder-hermes-key",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert "ubuntu-vscode-openwork" not in template_replacements_by_name
    assert "ubuntu-vscode-pi-web" not in template_replacements_by_name


def test_push_default_template_ignores_missing_terraform_lockfile(
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
            "--ignore-lockfile",
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
            "DOKPLOY_ADMIN_EMAIL": "adminuser@example.com",
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
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())

    notes = backend.ensure_application_ready()

    assert waits == ["coder.example.com"]
    assert secret_sync_calls == [
        (
                "dokploy-litellm",
                "opencode-go/deepseek-v4.1-flash",
                "http://wizard-stack-shared-litellm:4000",
        )
    ]
    assert notes == (
        "Provisioned initial Coder admin for 'admin@example.com'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes-desktop'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-hermes-pi'.",
        "Seeded default Coder template 'ubuntu-vscode'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-web'.",
        "Seeded default Coder template 'ubuntu-vscode-kdense-byok'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes'.",
    )


def test_ensure_application_ready_is_idempotent_on_second_bootstrap_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@openmerge.me",
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
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())

    first_notes = backend.ensure_application_ready()
    second_notes = backend.ensure_application_ready()

    expected_template_names = {
        coder_module._default_template_name(),
        coder_module._opencode_hermes_pi_template_name(),
        coder_module._base_template_name(),
        coder_module._default_opencode_web_template_name(),
        coder_module._default_kdense_byok_template_name(),
        coder_module._default_hermes_template_name(),
    }
    assert first_user_calls == [("coder.example.com", "admin@openmerge.me", "ChangeMeSoon")]
    assert set(template_copy_calls) == expected_template_names
    assert len(template_copy_calls) == len(expected_template_names)
    assert {name for name, _ in template_push_calls} == expected_template_names
    assert len(template_push_calls) == len(expected_template_names)
    assert all(version_name and version_name.startswith("dokploy-wizard-") for _, version_name in template_push_calls)
    assert created_workspaces == [
        ("openmergeme-workspace-2026-04-18", coder_module._default_template_name())
    ]
    assert first_notes == (
        "Provisioned initial Coder admin for 'admin@openmerge.me'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes-desktop'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-hermes-pi'.",
        "Seeded default Coder template 'ubuntu-vscode'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-web'.",
        "Seeded default Coder template 'ubuntu-vscode-kdense-byok'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes'.",
        "Created default Coder workspace 'openmergeme-workspace-2026-04-18' for 'admin@openmerge.me'.",
    )
    assert second_notes == ()


def _coder_backend_for_template_failure_tests() -> DokployCoderBackend:
    return DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="admin@openmerge.me",
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


def test_ensure_application_ready_skips_optional_template_push_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _coder_backend_for_template_failure_tests()
    _patch_coder_template_failure_bootstrap(monkeypatch, backend)
    attempted_templates: list[str] = []

    def fake_seed_template(*, template_name: str, **kwargs: object) -> bool:
        attempted_templates.append(template_name)
        if template_name == coder_module._default_kdense_byok_template_name():
            raise coder_module.CoderError(
                "Unable to push default Coder template 'ubuntu-vscode-kdense-byok': "
                "Terraform registry timeout at https://registry.coder.com/.well-known/terraform.json "
                "CODER_SESSION_TOKEN=session-123 API_KEY=sk-secret "
                + "x" * 500
            )
        return True

    monkeypatch.setattr(coder_module, "_seed_template", fake_seed_template)

    notes = backend.ensure_application_ready()

    assert attempted_templates == list(coder_module._required_template_names())
    skipped_notes = [note for note in notes if note.startswith("Skipped optional Coder template")]
    assert len(skipped_notes) == 1
    skipped_note = skipped_notes[0]
    assert "ubuntu-vscode-kdense-byok" in skipped_note
    assert "registry.coder.com" in skipped_note
    assert "sk-secret" not in skipped_note
    assert "session-123" not in skipped_note
    assert "<REDACTED>" in skipped_note
    # The note stays bounded so a failed push cannot bloat the state file, but a
    # terraform diagnosis has to survive it: the reason arrives at the end of the CLI's
    # output, after the banner and the streamed plan.
    assert len(skipped_note) < 1400


def test_ensure_application_ready_keeps_base_template_push_failure_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _coder_backend_for_template_failure_tests()
    _patch_coder_template_failure_bootstrap(monkeypatch, backend)
    attempted_templates: list[str] = []

    def fake_seed_template(*, template_name: str, **kwargs: object) -> bool:
        attempted_templates.append(template_name)
        raise coder_module.CoderError("base registry outage")

    monkeypatch.setattr(coder_module, "_seed_template", fake_seed_template)

    with pytest.raises(coder_module.CoderError, match="base registry outage"):
        backend.ensure_application_ready()

    assert attempted_templates == [coder_module._default_template_name()]


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
        admin_email="admin@openmerge.me",
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
        or template_replacements_by_name.setdefault(template_name, replacements),
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
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())

    notes = backend.ensure_application_ready()

    assert first_user_calls == [("coder.example.com", "admin@openmerge.me", "ChangeMeSoon")]
    assert login_calls == [("coder.example.com", "admin@openmerge.me", "ChangeMeSoon")]
    assert template_copy_calls == [
        (
            "wizard-stack-coder-container",
            str(coder_module._default_template_dir()),
            coder_module._default_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            str(coder_module._opencode_hermes_pi_template_dir()),
            coder_module._opencode_hermes_pi_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            str(coder_module._base_template_dir()),
            coder_module._base_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            str(coder_module._default_opencode_web_template_dir()),
            coder_module._default_opencode_web_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            str(coder_module._default_kdense_byok_template_dir()),
            coder_module._default_kdense_byok_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            str(coder_module._default_hermes_template_dir()),
            coder_module._default_hermes_template_name(),
        ),
    ]
    assert template_push_calls == [
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._default_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._opencode_hermes_pi_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._base_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._default_opencode_web_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._default_kdense_byok_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._default_hermes_template_name(),
        ),
    ]
    assert template_replacements_by_name[coder_module._default_kdense_byok_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__": "$${LITELLM_VIRTUAL_KEY_CODER_KDENSE}",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_opencode_web_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": "opencode-go",
        "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": "deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": "",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_hermes_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__": "dokploy-litellm",
        "__DOKPLOY_WIZARD_HERMES_MODEL__": "opencode-go/deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_HERMES_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_HERMES_API_KEY__": "",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert template_replacements_by_name[coder_module._default_hermes_desktop_template_name()] == {
        "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": "wizard-stack-shared",
        "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__": "dokploy-litellm",
        "__DOKPLOY_WIZARD_HERMES_MODEL__": "opencode-go/deepseek-v4.1-flash",
        "__DOKPLOY_WIZARD_HERMES_BASE_URL__": "http://wizard-stack-shared-litellm:4000",
        "__DOKPLOY_WIZARD_HERMES_API_KEY__": "",
        # The webapp needs no hostname token: Coder's subdomain app routing derives the
        # app hostname itself and the dashboard accepts any Host (measured on the box),
        # so this template gets the Coder origin for the community WebUI and nothing more.
        "__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__": "https://coder.example.com",
        # Both tokens: the clone kept the opencode serve password from its parent and
        # added the webapp sign-in password. An unreplaced token fails nothing — it
        # silently becomes the literal password.
        "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__": "ChangeMeSoon",
        "__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__": "ChangeMeSoon",
        # The browser-hosted Desktop has not shipped in a release, so this template
        # installs the channel that carries it. Only this one: an unreleased channel is
        # not a decision to make on behalf of the other Hermes templates.
        "__DOKPLOY_WIZARD_HERMES_INSTALL_CHANNEL__": "main",
        "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": _expected_coder_fallback_models_json_escaped(),
    }
    assert "ubuntu-vscode-openwork" not in template_replacements_by_name
    assert "ubuntu-vscode-pi-web" not in template_replacements_by_name
    assert ensure_workspace_calls == [
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            "openmergeme-workspace-2026-04-18",
            coder_module._default_template_name(),
        )
    ]
    assert secret_sync_calls == [
        (
            "wizard-stack-coder-container",
            "dokploy-litellm",
            "opencode-go/deepseek-v4.1-flash",
            None,
        )
    ]
    assert notes == (
        "Provisioned initial Coder admin for 'admin@openmerge.me'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes-desktop'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-hermes-pi'.",
        "Seeded default Coder template 'ubuntu-vscode'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-web'.",
        "Seeded default Coder template 'ubuntu-vscode-kdense-byok'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes'.",
        "Created default Coder workspace 'openmergeme-workspace-2026-04-18' for 'admin@openmerge.me'.",
    )


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
