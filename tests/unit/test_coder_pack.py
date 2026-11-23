# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import dokploy_wizard.dokploy.coder as coder_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.coder import DokployCoderApi, DokployCoderBackend, _render_compose_file
from dokploy_wizard.litellm.admin import WORKSPACE_KEY_DENY_MODEL, WORKSPACE_KEY_GRANTED_MODELS
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


@pytest.fixture(autouse=True)
def identity_timer(monkeypatch: pytest.MonkeyPatch):
    """Keep the host systemd timer out of coder-apply tests by default.

    The installer writes /etc/systemd/system and shells out to systemctl, so
    asserting on apply notes must not depend on who runs the suite. Timer
    tests take the fixture value (the real installer) and drive it against a
    tmp state dir themselves.
    """
    real = coder_module._install_identity_timer
    monkeypatch.setattr(coder_module, "_install_identity_timer", lambda **kwargs: ([], False))
    return real


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
    # Post-collapse the default is the only required template; its retired
    # predecessors are retirement targets, not push targets.
    assert coder_module._required_template_names() == (coder_module._default_template_name(),)
    assert "ubuntu-vscode" in coder_module._retired_template_names()
    assert "ubuntu-vscode-opencode-hermes-pi" in coder_module._retired_template_names()
    for retired_dir in (
        "default-ubuntu-code-server",
        "default-ubuntu-code-server-opencode-web",
        "default-ubuntu-code-server-hermes",
        "default-ubuntu-code-server-kdense-byok",
        "default-ubuntu-vscode-opencode-hermes-pi",
    ):
        assert not Path(f"templates/coder/{retired_dir}").exists()


def test_consolidated_template_apps_are_desktop_server_opencode_hermes_terminal() -> None:
    template = Path(
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
    ).read_text(encoding="utf-8")

    # VS Code browser + terminal + desktop come from the official code-server module.
    assert 'source   = "registry.coder.com/coder/code-server/coder"' in template
    assert 'slug         = "opencode"' in template
    assert 'slug         = "hermes-webui"' in template
    # Classic webui only: no ekko webui, no hermes dashboard surface anywhere.
    # Judged on executable lines only: the header comment names the dashboard to
    # say this is not it, and a guard that bans the words would ban the explanation.
    code = "\n".join(line for line in template.splitlines() if not line.lstrip().startswith("#"))
    assert 'slug         = "hermes-web-ui"' not in code
    assert 'slug         = "hermes-dashboard"' not in code
    assert "npm install -g hermes-web-ui" not in code
    assert "hermes dashboard" not in code



@dataclass
class FakeCoderBackend:
    existing_service: CoderResourceRecord | None = None
    existing_data: CoderResourceRecord | None = None
    health_ok: bool = True
    health_results: list[bool] | None = None
    ensure_calls: int = 0
    # Protocol member the lifecycle engine harvests after the coder apply;
    # this fake installs no host units.
    apply_time_owned_resources: tuple[OwnedResource, ...] = ()

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


def test_consolidated_template_serves_opencode_web_and_classic_hermes_with_cors_origin() -> None:
    template = Path(
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
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
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
    ).read_text(encoding="utf-8")

    # Pickers price models from models.dev (dollars per million tokens, the
    # unit opencode's UI bills in). Flat sync/<provider>-<model> aliases carry
    # their price in the gateway's own /v1/models model_info, so the registry
    # prefix map only needs passthrough aliases; ":free" aliases are always
    # priced at zero and shown as "(free)".
    assert 'MODELS_DEV_URL = "https://models.dev/api.json"' in template
    assert 'fetched_model_costs.get(alias)' in template
    assert '"input_cost_per_token"' in template
    assert '"sync/nvidia/": "nvidia",' not in template
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
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
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




def test_required_template_names_cover_only_remaining_templates() -> None:
    assert coder_module._required_template_names() == (
        coder_module._default_template_name(),
    )
    # The collapse: exactly one seeded template; the workspace default is it.
    assert coder_module._required_template_names() == (
        coder_module._default_hermes_desktop_template_name(),
    )


def test_retired_template_names_cover_removed_templates() -> None:
    assert coder_module._retired_template_names() == (
        "ubuntu-vscode-openwork",
        "ubuntu-vscode-pi-web",
        "ubuntu-vscode-hermes",
        "ubuntu-vscode",
        "ubuntu-vscode-opencode-web",
        "ubuntu-vscode-kdense-byok",
        "ubuntu-vscode-opencode-hermes-pi",
    )
    assert set(coder_module._retired_template_names()).isdisjoint(
        coder_module._required_template_names()
    )


def test_stable_serve_password_self_heals_a_placeholder_left_by_an_older_build() -> None:
    # A replacement token that is never replaced does not fail a plan: the literal
    # string becomes the password, which is exactly what one released build shipped
    # (the stored file was 43 bytes, the length of the token). Workspaces keep that
    # file across rebuilds, so the block has to recognize and rewrite it.
    for name in ("default-ubuntu-vscode-hermes-desktop",):
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
            {"name": coder_module._default_template_name()},
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
        return ({"name": coder_module._default_template_name()},)

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
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
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
    monkeypatch.setattr(
        coder_module,
        "_push_default_template",
        lambda *, template_name, **kwargs: template_push_calls.append(template_name),
    )
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: ensure_workspace_calls.append(kwargs)
        or coder_module.DefaultWorkspaceOutcome(action="exists"),
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )

    notes = backend.ensure_application_ready()

    assert template_push_calls == [coder_module._default_template_name()]
    # The adopt-or-create lane rides the healthy path too (upgraded boxes
    # converge their legacy names there); the stub reports the stable name
    # already exists, so no workspace note is emitted.
    assert len(ensure_workspace_calls) == 1
    assert ensure_workspace_calls[0]["workspace_name"] == "examplecom-workspace"
    assert ensure_workspace_calls[0]["template_name"] == coder_module._default_template_name()
    assert notes == ("Seeded default Coder template 'ubuntu-vscode-hermes-desktop'.",)
    consolidated_replacements = template_replacements_by_name[coder_module._default_template_name()]
    assert consolidated_replacements["__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__"] == "https://coder.example.com"
    assert consolidated_replacements["__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__"] == "ChangeMeSoon"
    assert consolidated_replacements["__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__"] == "wizard-stack-shared"
    assert consolidated_replacements["__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"] == (
        _expected_coder_fallback_models_json_escaped()
    )
    assert "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__" in consolidated_replacements
    # The collapse means one dict, so a stale predecessor must not sneak back in.
    assert set(template_replacements_by_name) == {coder_module._default_template_name()}
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
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(action="exists"),
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )

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
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )
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
        lambda hostname: "openmergeme-workspace",
    )
    monkeypatch.setattr(coder_module, "_list_workspaces", lambda **kwargs: tuple(sorted(workspaces)))

    def fake_create_default_workspace(*, workspace_name: str, template_name: str, **kwargs: object) -> None:
        created_workspaces.append((workspace_name, template_name))
        workspaces.add(workspace_name)

    monkeypatch.setattr(coder_module, "_create_default_workspace", fake_create_default_workspace)
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )

    first_notes = backend.ensure_application_ready()
    second_notes = backend.ensure_application_ready()

    expected_template_names = {
        coder_module._default_template_name(),
    }
    assert first_user_calls == [("coder.example.com", "admin@openmerge.me", "ChangeMeSoon")]
    assert set(template_copy_calls) == expected_template_names
    assert len(template_copy_calls) == len(expected_template_names)
    assert {name for name, _ in template_push_calls} == expected_template_names
    assert len(template_push_calls) == len(expected_template_names)
    assert all(version_name and version_name.startswith("dokploy-wizard-") for _, version_name in template_push_calls)
    assert created_workspaces == [
        ("openmergeme-workspace", coder_module._default_template_name())
    ]
    assert first_notes == (
        "Provisioned initial Coder admin for 'admin@openmerge.me'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes-desktop'.",
        "Created default Coder workspace 'openmergeme-workspace' for 'admin@openmerge.me'.",
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
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(action="exists"),
    )


def test_ensure_application_ready_reports_default_workspace_adoption_and_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _coder_backend_for_template_failure_tests()
    _patch_coder_template_failure_bootstrap(monkeypatch, backend)
    monkeypatch.setattr(
        coder_module, "_seed_template", lambda *, template_name, **kwargs: False
    )
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(
            action="adopted",
            adopted_from="openmergeme-workspace-2026-11-01",
            pruned=("openmergeme-workspace-2026-10-31",),
        ),
    )

    notes = backend.ensure_application_ready()

    assert (
        "Renamed legacy default Coder workspace 'openmergeme-workspace-2026-11-01' "
        "to 'examplecom-workspace'."
    ) in notes
    assert (
        "Pruned duplicate wizard-created Coder workspace 'openmergeme-workspace-2026-10-31'."
    ) in notes



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
    assert coder_module._default_workspace_name("coder.yourwebsite.com") == (
        "yourwebsitecom-workspace"
    )
    assert coder_module._default_workspace_name("coder.openmerge.me") == "openmergeme-workspace"
    # Stable across calls: no date component, so a post-midnight rerun cannot mint
    # a second default workspace.
    assert coder_module._default_workspace_name("coder.openmerge.me") == (
        coder_module._default_workspace_name("coder.openmerge.me")
    )


def test_legacy_default_workspace_names_recognizes_pre_stable_pattern() -> None:
    names = (
        "openmergeme-workspace-2026-10-31",
        "openmergeme-workspace-2026-11-01",
        "openmergeme-workspace",
        "openmergeme-workspace-latest",
        "somebody-else-workspace-2026-11-01",
    )

    assert coder_module._legacy_default_workspace_names("coder.openmerge.me", names) == (
        "openmergeme-workspace-2026-10-31",
        "openmergeme-workspace-2026-11-01",
    )

    # The legacy root was clamped to the 11 characters the dated suffix left room
    # for; adoption must recognize the truncated form, not the full token.
    assert coder_module._legacy_default_workspace_names(
        "coder.yourwebsite.com", ("yourwebsite-workspace-2026-11-01",)
    ) == ("yourwebsite-workspace-2026-11-01",)


def test_ensure_default_workspace_creates_missing_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coder_module,
        "_update_workspace_if_outdated",
        lambda **kwargs: False,
    )
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
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )

    assert created_workspace.action == "created"
    assert created_workspace.adopted_from is None
    assert created_workspace.pruned == ()
    assert created == [
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            "examplecom-workspace",
            "ubuntu-vscode",
        )
    ]


def test_ensure_default_workspace_skips_existing_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        coder_module,
        "_update_workspace_if_outdated",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: ("examplecom-workspace",),
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
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )

    assert created_workspace.action == "exists"
    assert created_workspace.pruned == ()
    assert create_calls == []


def test_ensure_default_workspace_adopts_newest_legacy_dated_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-midnight duplicate bug must converge, not mint a third stack."""

    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: (
            "examplecom-workspace-2026-10-31",
            "examplecom-workspace-2026-11-01",
        ),
    )
    create_calls: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_create_default_workspace",
        lambda **kwargs: create_calls.append("called"),
    )
    monkeypatch.setattr(
        coder_module,
        "_workspace_template_name",
        lambda **kwargs: "ubuntu-vscode",
    )
    renamed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coder_module,
        "_rename_workspace",
        lambda *, workspace_name, new_workspace_name, **kwargs: renamed.append(
            (workspace_name, new_workspace_name)
        ),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_delete_workspace",
        lambda *, workspace_name, **kwargs: deleted.append(workspace_name),
    )

    outcome = coder_module._ensure_default_workspace(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )

    assert deleted == ["examplecom-workspace-2026-10-31"]
    # Newest date is adopted; the older duplicate is pruned; nothing is created.
    assert outcome.action == "adopted"
    assert outcome.adopted_from == "examplecom-workspace-2026-11-01"
    assert outcome.pruned == ("examplecom-workspace-2026-10-31",)
    assert renamed == [("examplecom-workspace-2026-11-01", "examplecom-workspace")]
    assert create_calls == []


def test_ensure_default_workspace_prunes_duplicates_when_stable_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: (
            "examplecom-workspace",
            "examplecom-workspace-2026-10-31",
            "examplecom-workspace-2026-11-01",
        ),
    )

    def fake_template_name(*, workspace_name: str, **kwargs: object) -> str | None:
        del workspace_name, kwargs
        return "ubuntu-vscode"

    monkeypatch.setattr(coder_module, "_workspace_template_name", fake_template_name)
    create_calls: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_create_default_workspace",
        lambda **kwargs: create_calls.append("called"),
    )
    renamed: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_rename_workspace",
        lambda *, workspace_name, **kwargs: renamed.append(workspace_name),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_delete_workspace",
        lambda *, workspace_name, **kwargs: deleted.append(workspace_name),
    )

    outcome = coder_module._ensure_default_workspace(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )

    assert outcome.action == "exists"
    assert outcome.pruned == ("examplecom-workspace-2026-10-31", "examplecom-workspace-2026-11-01")
    assert deleted == ["examplecom-workspace-2026-10-31", "examplecom-workspace-2026-11-01"]
    assert renamed == []
    assert create_calls == []


def test_ensure_default_workspace_leaves_foreign_template_legacy_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dated names the wizard did not seed (foreign template) are the user's."""

    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: ("examplecom-workspace-2026-10-31",),
    )
    monkeypatch.setattr(
        coder_module,
        "_workspace_template_name",
        lambda **kwargs: "my-custom-template",
    )
    create_calls: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_create_default_workspace",
        lambda **kwargs: create_calls.append("called"),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_delete_workspace",
        lambda *, workspace_name, **kwargs: deleted.append(workspace_name),
    )

    outcome = coder_module._ensure_default_workspace(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )

    # Not adoptable, not prunable: create the stable workspace and leave it alone.
    assert outcome.action == "created"
    assert outcome.pruned == ()
    assert deleted == []
    assert create_calls == ["called"]


def test_ensure_default_workspace_adopts_when_template_shape_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI output shape change must not resurrect the dated-duplicate bug."""

    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: ("examplecom-workspace-2026-11-01",),
    )
    monkeypatch.setattr(
        coder_module,
        "_workspace_template_name",
        lambda **kwargs: None,
    )
    renamed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coder_module,
        "_rename_workspace",
        lambda *, workspace_name, new_workspace_name, **kwargs: renamed.append(
            (workspace_name, new_workspace_name)
        ),
    )

    outcome = coder_module._ensure_default_workspace(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )

    assert outcome.action == "adopted"
    assert renamed == [("examplecom-workspace-2026-11-01", "examplecom-workspace")]


def test_coder_backend_reads_filex_provisioner_lazily_from_state(tmp_path: Path) -> None:
    """The lifecycle builds backends before any phase runs, but the FileX phase
    creates its provisioner document before the Coder phase pushes the template.
    The backend therefore re-reads the state directory at push time — otherwise
    a first-run install bakes no door and the ~/shared mount only appears on a
    second pass."""
    from dokploy_wizard.state.models import STATE_FORMAT_VERSION, FilexProvisionerState
    from dokploy_wizard.state.store import write_filex_provisioner

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
        state_dir=tmp_path,
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    # Nothing on disk yet: no door, no token (the pack is off or unreconciled).
    assert backend._filex_internal_url() == ""
    assert backend._filex_provisioner_state() is None

    write_filex_provisioner(
        tmp_path,
        FilexProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username="wizard-provisioner",
            email="wizard-provisioner@filex.example.com",
            password="pw",
            user_id=7,
            token="prov-token-9",
            token_id=11,
        ),
    )
    # The document appeared mid-run (FileX phase before Coder phase): the push
    # sees it without the backend being rebuilt.
    assert backend._filex_internal_url() == "http://wizard-stack-filex:5212"
    assert backend._filex_provisioner_state() is not None
    assert backend._filex_provisioner_state().token == "prov-token-9"


def test_coder_backend_reads_gitea_provisioner_lazily_from_state(tmp_path: Path) -> None:
    """Same mid-run convergence as FileX: the Gitea phase seeds its provisioner
    before the Coder phase pushes the template in one process, so the backend
    re-reads the state directory at push time — otherwise a first-run install
    bakes no Gitea door and the identity lane only appears on a second pass."""
    from dokploy_wizard.state.models import STATE_FORMAT_VERSION, GiteaProvisionerState
    from dokploy_wizard.state.store import write_gitea_provisioner

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
        state_dir=tmp_path,
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    assert backend._gitea_internal_url() == ""
    assert backend._gitea_provisioner_state() is None

    write_gitea_provisioner(
        tmp_path,
        GiteaProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username="dokploy-wizard",
            email="dokploy-wizard@gitea.example.com",
            password="pw",
            token="gt-token-9",
        ),
    )
    assert backend._gitea_internal_url() == "http://wizard-stack-gitea:3000"
    assert backend._gitea_provisioner_state() is not None
    assert backend._gitea_provisioner_state().token == "gt-token-9"


def test_identity_sync_actions_scopes_to_wizard_sprung_accounts() -> None:
    # Scope is the state ledger plus this-pass claims: a row is wizard-owned
    # when its username AND primary email exactly match a current Coder user,
    # or when the ledger already carries it. Manual accounts (a different
    # email, never claimed), the drifting pre-fix rows, and the provisioner
    # itself are never touched.
    rows = [
        {"username": "clayton", "email": "clayton@superiorbyteworks.com", "active": True},
        {"username": "ghost", "email": "ghost@corp.example", "active": True},
        {"username": "returner", "email": "returner@corp.example", "active": False},
        {"username": "drifted", "email": "drifted@coder.wizard.local", "active": True},
        {"username": "operator-manual", "email": "ops@example.com", "active": True},
        {"username": "dokploy-wizard", "email": "dokploy-wizard@gitea.example.com", "active": True},
    ]
    coder_users = {
        "clayton": "clayton@superiorbyteworks.com",  # match: claimed, stays active
        "returner": "returner@corp.example",  # tracked + present: re-enable
        "drifted": "drifted@corp.example",  # email moved on: untracked row is not ours
    }

    actions, claimed = coder_module._identity_sync_actions(
        gitea_rows=rows,
        coder_users=coder_users,
        provisioner_username="dokploy-wizard",
        tracked={"ghost", "returner"},
    )

    # Present+active stays; tracked+deleted gets disabled; returned gets re-enabled.
    assert actions == [("ghost", False), ("returner", True)]
    # Claims land in the ledger for the next pass; drift and manual stay out.
    assert claimed == {"ghost", "returner", "clayton"}


def test_identity_sync_actions_treats_deleted_status_as_absent() -> None:
    actions, claimed = coder_module._identity_sync_actions(
        gitea_rows=[{"username": "gone", "email": "gone@corp.example", "active": True}],
        coder_users={},  # caller already dropped status == "deleted" rows
        provisioner_username="dokploy-wizard",
        tracked={"gone"},
    )
    # Measured: deleted Coder users are unqueryable, so the ledger's memory
    # is what lets a still-active Gitea account be recognised as ours and
    # disabled at all.
    assert actions == [("gone", False)]
    assert claimed == {"gone"}


def test_identity_sync_actions_untracks_fully_vanished_claims() -> None:
    # The demo-prop teardown lane: an operator deletes the Gitea account
    # outright, and the Coder user is gone too. A claim over an account that
    # exists nowhere is ledger noise — the next full reseed re-claims it when
    # the boot lane springs a fresh row. But a tracked login whose Coder user
    # still lives (workspace mid-boot, Gitea not yet sprung) must NOT be
    # dropped: that gap is exactly what the claim exists to survive.
    actions, claimed = coder_module._identity_sync_actions(
        gitea_rows=[
            {"username": "returning", "email": "r@corp.example", "active": True},
        ],
        coder_users={"returning": "r@corp.example", "respawn": "s@corp.example"},
        provisioner_username="dokploy-wizard",
        tracked={"demo", "respawn", "returning"},
    )
    assert actions == []  # 'demo' has no row to disable; nothing else differs
    assert claimed == {"returning", "respawn"}  # 'demo' fully gone: untracked


def test_sync_gitea_identities_patches_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, object]] = []

    def fake_coder_request(**kwargs: object) -> dict[str, object]:
        assert kwargs["method"] == "GET"
        assert kwargs["path"] == "/api/v2/users"
        # Measured at the v2.39.4 pin: this header spelling is the one Coder
        # accepts on the users API.
        assert kwargs["session_token"] == "session-123"
        return {
            "users": [
                {"username": "clayton", "email": "clayton@corp.example", "status": "active"},
                {"username": "returner", "email": "returner@corp.example", "status": "dormant"},
                {"username": "purged", "email": "purged@corp.example", "status": "deleted"},
            ]
        }

    def fake_gitea_request(**kwargs: object) -> tuple[int | None, object]:
        calls.append((str(kwargs["method"]), str(kwargs["path"]), kwargs.get("payload") or {}))
        assert kwargs["token"] == "gt-token-9"
        if kwargs["method"] == "GET":
            return 200, [
                {
                    "username": "dokploy-wizard",
                    "email": "dokploy-wizard@gitea.example.com",
                    "active": True,
                },
                {"username": "clayton", "email": "clayton@corp.example", "active": True},
                {"username": "purged", "email": "purged@corp.example", "active": True},
                {"username": "returner", "email": "returner@corp.example", "active": False},
            ]
        return 200, None

    monkeypatch.setattr(coder_module, "_coder_request", fake_coder_request)
    monkeypatch.setattr(coder_module, "_gitea_api_request", fake_gitea_request)

    # The ledger starts holding the account the lane sprung before its Coder
    # user was deleted (purged) and the one that later returned; clayton is a
    # fresh claim (username + email match the live roster).
    notes, claimed = coder_module._sync_gitea_identities(
        coder_hostname="coder.example.com",
        session_token="session-123",
        gitea_hostname="gitea.example.com",
        provisioner_token="gt-token-9",
        provisioner_username="dokploy-wizard",
        tracked={"purged", "returner"},
    )

    assert notes == [
        "Disabled Gitea account 'purged' (Coder user deleted).",
        "Re-enabled Gitea account 'returner' (Coder user returned).",
    ]
    # purged/returner carry over, clayton is claimed this pass.
    assert claimed == {"purged", "returner", "clayton"}
    assert calls[0] == ("GET", "/api/v1/admin/users?page=1", {})
    assert ("PATCH", "/api/v1/admin/users/purged", {"active": False}) in calls
    assert ("PATCH", "/api/v1/admin/users/returner", {"active": True}) in calls


def test_sync_gitea_identities_raises_when_patch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A rejected PATCH must surface, not vanish: the caller degrades it to a
    # WARNING note, and the next pass retries — a silently ignored 405 would
    # pin the account in the wrong state forever.
    monkeypatch.setattr(
        coder_module,
        "_coder_request",
        lambda **kwargs: {"users": []},
    )
    monkeypatch.setattr(
        coder_module,
        "_gitea_admin_user_rows",
        lambda **kwargs: [{"username": "ghost", "email": "ghost@corp.example", "active": True}],
    )
    monkeypatch.setattr(
        coder_module,
        "_gitea_api_request",
        lambda **kwargs: (405, {"message": "Method Not Allowed"}),
    )

    with pytest.raises(coder_module.CoderError):
        coder_module._sync_gitea_identities(
            coder_hostname="coder.example.com",
            session_token="session-123",
            gitea_hostname="gitea.example.com",
            provisioner_token="gt-token-9",
            provisioner_username="dokploy-wizard",
            tracked={"ghost"},
        )


def test_gitea_identity_lane_is_baked_into_the_desktop_template() -> None:
    template = Path(
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
    ).read_text(encoding="utf-8")

    # Guard + sentinels follow the FileX trick: the door renders empty when
    # the pack is off, and the *_SENTINEL__ markers stay out of the map.
    assert 'DOKPLOY_WIZARD_GITEA_URL="__DOKPLOY_WIZARD_GITEA_INTERNAL_URL__"' in template
    assert (
        'DOKPLOY_WIZARD_GITEA_URL_SENTINEL="__DOKPLOY_WIZARD_GITEA_INTERNAL_URL_SENTINEL__"'
        in template
    )
    assert "<<'GITEAPROV'" in template
    # Account spring through the admin API under the REAL coder email (identity
    # fix: no coder.wizard.local primary), must-change OFF (measured: a pending
    # must-change turns basic-auth API calls into 403), and the legacy
    # convention email is retained only as the migration marker.
    assert 'email = os.environ.get("DOKPLOY_GT_REAL_EMAIL", "").strip().lower()' in template
    assert 'legacy_email = login + "@coder.wizard.local"' in template
    assert '"must_change_password": False' in template
    assert '"/api/v1/admin/users", provisioner_auth' in template
    # Box-measured: GET /api/v1/admin/users/<name> is NOT routed (405; missing
    # names 404 from the redirect middleware), so the lane probes existence
    # through the paginated collection list instead.
    assert '"/api/v1/admin/users?page=%d" % page' in template
    assert "if len(rows) < 30" in template
    # The account password is the stack admin password (one operator
    # credential across the stack), baked from the replacement map and
    # re-synced through the admin API on every boot for wizard-owned accounts.
    assert (
        'DOKPLOY_WIZARD_GITEA_ACCOUNT_PASSWORD="__DOKPLOY_WIZARD_GITEA_ACCOUNT_PASSWORD__"'
        in template
    )
    assert 'password = os.environ["DOKPLOY_GT_PASSWORD"]' in template
    assert '"password": password, "must_change_password": False' in template
    # The owner's real email IS the primary login identity from creation
    # (box-measured on 28.4.0: an admin-API create with a real email returns it
    # verified+primary and it authenticates basic-auth). A pre-fix spring
    # (legacy primary) is migrated through PATCH /admin/users (200) and a
    # taken email (409) is tolerated, not fatal — the real email already rides
    # the account as a verified secondary that logs in.
    assert 'export DOKPLOY_GT_REAL_EMAIL="${data.coder_workspace_owner.me.email}"' in template
    assert '"/api/v1/admin/users/" + user_path, provisioner_auth, {"email": email}' in template
    assert "elif status == 409:" in template
    # One key per (user, workspace): title is the shared label, adoption is by
    # key material, replacement goes through the owner's own credentials.
    assert 'title = login + "-" + workspace' in template
    assert '/api/v1/users/" + user_path + "/keys' in template
    assert '/api/v1/admin/users/" + user_path + "/keys' in template
    assert '/api/v1/user/keys/%s' in template
    # ssh convenience over the overlay door, and git identity defaults.
    assert '"# BEGIN dokploy-wizard gitea"' in template
    assert "ssh-keyscan" in template
    assert '"git", "config", "--global", "user.name", login' in template
    # The private demo seed: created through the OWNER door (basic auth —
    # the admin token's owner field does not steer POST /api/v1/user/repos,
    # measured), explicit private, adopted untouched when it already stands,
    # and the seed push only ever lands in an empty repo over the 'gitea'
    # SSH alias with the workspace's own key.
    assert 'demo = "wizard-demo"' in template
    assert '"/api/v1/repos/" + user_path + "/" + demo, owner_basic' in template
    assert '"POST", "/api/v1/user/repos", owner_basic' in template
    assert '"name": demo, "private": True' in template
    assert 'note("adopted the existing \'" + demo + "\' repo")' in template
    assert 'repo.get("empty")' in template
    assert '"git@gitea:" + login + "/" + demo + ".git"' in template
    # The lane must never hold the boot hostage: backgrounded, log to file.
    assert ") > /tmp/gitea-identity.log 2>&1 &" in template


@pytest.mark.parametrize("sync_failure", (False, True))
def test_ensure_application_ready_runs_gitea_identity_sync_and_degrades_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_failure: bool,
) -> None:
    from dokploy_wizard.state.models import STATE_FORMAT_VERSION, GiteaProvisionerState

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
        gitea_provisioner=GiteaProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username="dokploy-wizard",
            email="dokploy-wizard@gitea.example.com",
            password="pw",
            token="gt-token-9",
        ),
        gitea_hostname="gitea.example.com",
        state_dir=tmp_path,
        client=cast(DokployCoderApi, FakeCoderApi()),
    )

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
    monkeypatch.setattr(coder_module, "_copy_template_into_container", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(action="exists"),
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(
        coder_module,
        "_coder_roster",
        lambda **kwargs: {"clayton": "clayton@example.com"},
    )
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )

    sync_calls: list[dict[str, object]] = []

    def fake_sync(**kwargs: object) -> tuple[list[str], set[str]]:
        sync_calls.append(dict(kwargs))
        if sync_failure:
            raise coder_module.CoderError("HTTP 500")
        return (
            ["Disabled Gitea account 'purged' (Coder user deleted)."],
            {"purged", "clayton"},
        )

    monkeypatch.setattr(coder_module, "_sync_gitea_identities", fake_sync)

    notes = backend.ensure_application_ready()

    # The provisioner document baked at construction (plus the pack hostname)
    # drives the sync directly: no re-read of a missing state file needed.
    # The ledger starts empty (no state file yet) and the sync's claim set is
    # persisted back so deletions stay visible on later passes.
    assert sync_calls == [
        {
            "coder_hostname": "coder.example.com",
            "session_token": "session-123",
            "gitea_hostname": "gitea.example.com",
            "provisioner_token": "gt-token-9",
            "provisioner_username": "dokploy-wizard",
            "tracked": set(),
            # The roster is read once per pass and handed to every surface.
            "coder_users": {"clayton": "clayton@example.com"},
            # The lane seeds missing accounts too, so the stack password rides along.
            "password": "ChangeMeSoon",
        }
    ]
    if sync_failure:
        assert any(note.startswith("Skipped Gitea identity sync:") for note in notes)
        assert "Disabled Gitea account 'purged' (Coder user deleted)." not in notes
        assert not (tmp_path / "gitea-identities.json").exists()
    else:
        assert "Disabled Gitea account 'purged' (Coder user deleted)." in notes
        from dokploy_wizard.state.store import load_gitea_identities

        ledger = load_gitea_identities(tmp_path)
        assert ledger is not None
        assert ledger.usernames == ("clayton", "purged")


def test_sync_filex_identities_pre_stands_free_logins_and_spares_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-flight mirrors the boot lane: email match adopts, any other
    owner of the username is left alone, only free logins are created."""
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(*, hostname, method, path, token, body=None):  # noqa: ANN001, ANN003
        calls.append((method, path, body))
        if method == "GET":
            return (
                200,
                {
                    "users": [
                        {"id": 1, "username": "person", "email": "Person@Example.com"},
                        {"id": 2, "username": "bob", "email": "bob@manual.test"},
                        {"id": 3, "username": "legacy", "email": "legacy@coder.wizard.local"},
                    ]
                },
            )
        return (201, {"id": 4})

    monkeypatch.setattr(coder_module, "_filex_api_request", fake_request)

    notes = coder_module._sync_filex_identities(
        hostname="files.example.com",
        provisioner_token="fx-token",
        coder_users={
            "person": "person@example.com",  # row already standing for the person
            "bob": "bob@example.com",  # manual account owns the login
            "legacy": "legacy@example.com",  # pre-fix twin row owns the login
            "carol": "carol@example.com",  # free login: created
        },
        password="StackPw123",
    )

    assert notes == ["Created FileX account 'carol' (identity pre-flight)."]
    creates = [call for call in calls if call[0] == "POST"]
    assert creates == [
        (
            "POST",
            "/api/admin/users",
            {
                "email": "carol@example.com",
                "username": "carol",
                "role": "user",
                "password": "StackPw123",
            },
        )
    ]


def test_sync_filex_identities_raises_when_list_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coder_module, "_filex_api_request", lambda **kwargs: (401, {"detail": "nope"})
    )
    with pytest.raises(coder_module.CoderError, match="HTTP 401"):
        coder_module._sync_filex_identities(
            hostname="files.example.com",
            provisioner_token="fx-token",
            coder_users={"bob": "bob@example.com"},
            password="pw",
        )


def test_ensure_application_ready_fans_identity_sync_out_and_isolates_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One roster, three surfaces: a broken surface notes, the others run."""
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
        state_dir=tmp_path,
        client=cast(DokployCoderApi, FakeCoderApi()),
    )
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: False)
    monkeypatch.setattr(coder_module, "_create_coder_first_user", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module, "_coder_container_name", lambda service_name: "container"
    )
    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_template_version_names", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_copy_template_into_container", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(action="exists"),
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )
    monkeypatch.setattr(
        coder_module,
        "_coder_roster",
        lambda **kwargs: {"clayton": "clayton@example.com"},
    )

    seen: list[str] = []

    def gitea_lane(session_token, coder_users=None):  # noqa: ANN001, ANN003
        seen.append("gitea")
        return ["Gitea lane note."]

    def filex_lane(coder_users):  # noqa: ANN001
        seen.append("filex")
        raise coder_module.CoderError("HTTP 503")

    def litellm_lane(coder_users):  # noqa: ANN001
        seen.append("litellm")
        return ["Created LiteLLM user 'clayton' (proxy_admin)."]

    monkeypatch.setattr(backend, "_sync_gitea_identities", gitea_lane)
    monkeypatch.setattr(backend, "_sync_filex_identities", filex_lane)
    monkeypatch.setattr(backend, "_sync_litellm_identities", litellm_lane)

    notes = backend.ensure_application_ready()

    assert seen == ["gitea", "filex", "litellm"]
    assert "Gitea lane note." in notes
    assert "Created LiteLLM user 'clayton' (proxy_admin)." in notes
    assert any(note.startswith("Skipped FileX identity sync:") for note in notes)


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
        or coder_module.DefaultWorkspaceOutcome(action="created"),
    )
    monkeypatch.setattr(
        coder_module,
        "_default_workspace_name",
        lambda hostname: "openmergeme-workspace",
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )

    notes = backend.ensure_application_ready()

    assert first_user_calls == [("coder.example.com", "admin@openmerge.me", "ChangeMeSoon")]
    assert login_calls == [("coder.example.com", "admin@openmerge.me", "ChangeMeSoon")]
    assert template_copy_calls == [
        (
            "wizard-stack-coder-container",
            str(coder_module._default_template_dir()),
            coder_module._default_template_name(),
        ),
    ]
    assert template_push_calls == [
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._default_template_name(),
        ),
    ]
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
        "__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY__": "",
        # This backend was built without a FileX provisioner: the pack is off,
        # so both door values render empty and the startup script skips ~/shared.
        "__DOKPLOY_WIZARD_FILEX_INTERNAL_URL__": "",
        "__DOKPLOY_WIZARD_FILEX_PROVISIONER_TOKEN__": "",
        # The FileX account password rides the stack admin password whenever
        # the lane is armed (the account IS the person, one credential).
        "__DOKPLOY_WIZARD_FILEX_ACCOUNT_PASSWORD__": "ChangeMeSoon",
        # Same for the Gitea identity lane: no provisioner document, so the
        # GITEAPROV boot lane is disabled and the coder phase runs no sync.
        "__DOKPLOY_WIZARD_GITEA_INTERNAL_URL__": "",
        "__DOKPLOY_WIZARD_GITEA_PROVISIONER_TOKEN__": "",
        # The Gitea account password rides the stack admin password whenever
        # the lane is armed (see the identity-lane template test).
        "__DOKPLOY_WIZARD_GITEA_ACCOUNT_PASSWORD__": "ChangeMeSoon",
        # Default policy lane: deny for new users, empty allowlist, the
        # seeded admin login derived from the admin email, and the sentinel
        # deny model the sweep and the template both recognize.
        "__DOKPLOY_WIZARD_LITELLM_ALLOW_NEW_USERS__": "false",
        "__DOKPLOY_WIZARD_LITELLM_ALLOWED_USERS__": "",
        "__DOKPLOY_WIZARD_LITELLM_ADMIN_LOGIN__": "admin",
        "__DOKPLOY_WIZARD_LITELLM_DENY_MODEL__": "dokploy-wizard-model-access-denied",
        "__DOKPLOY_WIZARD_LITELLM_GRANTED_MODELS__": "all-access",
    }
    assert "ubuntu-vscode-openwork" not in template_replacements_by_name
    assert "ubuntu-vscode-pi-web" not in template_replacements_by_name
    assert ensure_workspace_calls == [
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            "openmergeme-workspace",
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
        "Created default Coder workspace 'openmergeme-workspace' for 'admin@openmerge.me'.",
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

_GATEWAY_KEY_TEMPLATE_DIRS = (
    "default-ubuntu-vscode-hermes-desktop",
)


@pytest.mark.parametrize("template_dir", _GATEWAY_KEY_TEMPLATE_DIRS)
def test_workspace_key_mint_block_is_baked_into_gateway_key_templates(template_dir: str) -> None:
    template = Path(f"templates/coder/{template_dir}/main.tf").read_text(encoding="utf-8")

    # The issuer token survives as a sentinel when the wizard seeded no issuer,
    # and the guard rejects both the empty value and a sentinel that is never
    # substituted (comparing against a second copy of the replaced marker would
    # compare the real key to itself and skip minting forever).
    assert 'DOKPLOY_WIZARD_LITELLM_ISSUER_KEY="__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY__"' in template
    assert 'DOKPLOY_WIZARD_LITELLM_ISSUER_KEY_SENTINEL="__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY_SENTINEL__"' in template
    assert '"$DOKPLOY_WIZARD_LITELLM_ISSUER_KEY_SENTINEL" ' in template + " "
    assert "<<'MINTPY'" in template
    # Deterministic child token: issuer + the owner-scoped label, so restarts
    # adopt the existing key and issuer rotations re-mint without bookkeeping.
    # A bare workspace name would collide across owners (Coder names are
    # unique per owner), letting a second owner's "dev" adopt the first's key.
    assert 'token = "sk-" + hashlib.sha256((issuer + ":" + label).encode()).hexdigest()[:40]' in template
    assert 'label = owner + "-" + workspace' in template
    # The identity comes from Coder itself: owner AND workspace, so LiteLLM
    # spend attributes per person and machine by construction.
    assert '"${data.coder_workspace.me.name}"' in template
    assert '"${data.coder_workspace_owner.me.name}"' in template
    assert '"key_alias": "coder-" + label' in template
    assert '"user_id": owner,' in template
    assert '"coder_owner": owner' in template
    # The grant is the wizard's named access group, read from the baked
    # replacement marker (single source: WORKSPACE_KEY_GRANTED_MODELS); the
    # deny lane is a sentinel model the gateway does not deploy. The Python
    # sweep pins the same constant, so template and admin-side re-scope can
    # never drift apart silently.
    assert (
        'DOKPLOY_WIZARD_LITELLM_GRANTED_MODELS="__DOKPLOY_WIZARD_LITELLM_GRANTED_MODELS__"'
        in template
    )
    assert 'os.environ.get("DOKPLOY_WIZARD_LITELLM_GRANTED_MODELS", "").split(",")' in template
    assert '"models": models,' in template
    assert WORKSPACE_KEY_GRANTED_MODELS == ("all-access",)
    assert WORKSPACE_KEY_DENY_MODEL == "dokploy-wizard-model-access-denied"
    # Per-user model access policy (baked at seed time): the flag, the
    # allowlist, and the always-exempt seeded admin login. A denied owner
    # mints against the deny model instead of falling back to the shared
    # baked key, which would bypass the policy entirely.
    assert (
        'DOKPLOY_WIZARD_LITELLM_ALLOW_NEW_USERS="__DOKPLOY_WIZARD_LITELLM_ALLOW_NEW_USERS__"'
        in template
    )
    assert (
        'DOKPLOY_WIZARD_LITELLM_ALLOWED_USERS="__DOKPLOY_WIZARD_LITELLM_ALLOWED_USERS__"'
        in template
    )
    assert (
        'DOKPLOY_WIZARD_LITELLM_ADMIN_LOGIN="__DOKPLOY_WIZARD_LITELLM_ADMIN_LOGIN__"' in template
    )
    assert 'DOKPLOY_WIZARD_LITELLM_DENY_MODEL="__DOKPLOY_WIZARD_LITELLM_DENY_MODEL__"' in template
    assert 'or owner.lower() in allowed_users' in template
    assert 'admin_login and owner.lower() == admin_login' in template
    assert 'models = [deny_model]' in template
    # Info-then-generate with a final re-info: any refusal (permissions, alias
    # collision, gateway unreachable) leaves the variable empty and the baked
    # shared template key in play.
    assert 'mint_request("GET", "/key/info?key=" + token)' in template
    assert 'mint_request(' in template
    assert '|| DOKPLOY_WIZARD_WORKSPACE_API_KEY=""' in template
    assert "import hashlib, json, os, urllib.error, urllib.request" in template
    # Policy-mismatch lane: the issuer route set has /key/delete but no
    # /key/update, so a moved policy — or a key predating the coder- alias /
    # owner attribution — swaps the mint under the same deterministic token
    # (spend attribution survives the swap). Unknown fields are spared: only
    # a visible mismatch retires.
    assert 'current_models != sorted(str(item) for item in models)' in template
    assert 'current_alias != expected_alias' in template
    assert 'current_user != owner' in template
    assert 'mint_request("POST", "/key/delete", {"keys": [token]})' in template
    # Upgrade path: the pre owner-scoping token (issuer + bare workspace) is
    # deterministic too, so retire it once the scoped key stands — otherwise
    # the sweep's workspace-key spare strands the old-format key forever.
    assert (
        'old_token = "sk-" + hashlib.sha256((issuer + ":" + workspace).encode())'
        ".hexdigest()[:40]"
    ) in template
    assert 'mint_request("POST", "/key/delete", {"keys": [old_token]})' in template



def test_workspace_issuer_key_flows_into_gateway_key_template_replacements(
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
        workspace_issuer_api_key="sk-litellm-coder-workspace-issuer-provided",
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
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
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
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(action="exists"),
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(
        coder_module,
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
    )

    backend.ensure_application_ready()

    for name in (coder_module._default_template_name(),):
        replacements = template_replacements_by_name[name]
        assert replacements is not None
        assert (
            replacements["__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY__"]
            == "sk-litellm-coder-workspace-issuer-provided"
        )
        # Default policy: deny for new users, empty allowlist, the seeded
        # admin login derived from the admin email, and the sentinel deny
        # model the sweep and the template both recognize.
        assert replacements["__DOKPLOY_WIZARD_LITELLM_ALLOW_NEW_USERS__"] == "false"
        assert replacements["__DOKPLOY_WIZARD_LITELLM_ALLOWED_USERS__"] == ""
        assert replacements["__DOKPLOY_WIZARD_LITELLM_ADMIN_LOGIN__"] == "admin"
        assert replacements["__DOKPLOY_WIZARD_LITELLM_DENY_MODEL__"] == WORKSPACE_KEY_DENY_MODEL
    # Post-collapse there is exactly one seeded template; nothing else carries tokens.
    assert set(template_replacements_by_name) == {coder_module._default_template_name()}


def test_workspace_key_policy_replacements_bake_operator_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="coder.example.com",
        wildcard_hostname="*.coder.example.com",
        admin_email="clayton@superiorbyteworks.com",
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
        workspace_issuer_api_key="sk-litellm-coder-workspace-issuer-provided",
        litellm_allow_new_users=True,
        litellm_allowed_users=("Bob", "jo-ho"),
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
        "_migrate_retired_template_workspaces",
        lambda **kwargs: (),
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
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(action="exists"),
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})

    backend.ensure_application_ready()

    replacements = template_replacements_by_name[coder_module._default_template_name()]
    assert replacements is not None
    assert replacements["__DOKPLOY_WIZARD_LITELLM_ALLOW_NEW_USERS__"] == "true"
    # Logins normalize to lowercase at store time so the template's lowercase
    # comparison actually matches Coder's lowercased logins.
    assert replacements["__DOKPLOY_WIZARD_LITELLM_ALLOWED_USERS__"] == "bob,jo-ho"
    assert replacements["__DOKPLOY_WIZARD_LITELLM_ADMIN_LOGIN__"] == "clayton"
    assert replacements["__DOKPLOY_WIZARD_LITELLM_DENY_MODEL__"] == WORKSPACE_KEY_DENY_MODEL


def test_retirement_migrates_workspaces_off_retired_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The home volume is named off the workspace id, so retirement must re-base
    # workspaces in place; the two survivors here pin both halves of that rule.
    updated: list[tuple[str, str]] = []
    templates = {
        "on-retired": "ubuntu-vscode",
        "on-survivor": coder_module._default_template_name(),
        "on-operator-custom": "operator-custom-template",
    }

    monkeypatch.setattr(
        coder_module, "_list_workspaces", lambda **kwargs: tuple(sorted(templates))
    )
    monkeypatch.setattr(
        coder_module,
        "_workspace_template_name",
        lambda **kwargs: templates[kwargs["workspace_name"]],
    )
    monkeypatch.setattr(
        coder_module,
        "_update_workspace_template",
        lambda **kwargs: updated.append(
            (kwargs["workspace_name"], kwargs["template_name"])
        ),
    )

    migrated = coder_module._migrate_retired_template_workspaces(
        container_name="stack-coder",
        hostname="coder.example.com",
        session_token="tok",
        template_name=coder_module._default_template_name(),
    )

    assert migrated == ("on-retired",)
    assert updated == [("on-retired", coder_module._default_template_name())]


def test_workspace_template_migration_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            command, returncode=1, stdout="", stderr="error: workspace is locked"
        )

    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    with pytest.raises(coder_module.CoderError, match="workspace is locked"):
        coder_module._update_workspace_template(
            container_name="stack-coder",
            hostname="coder.example.com",
            session_token="tok",
            workspace_name="ws",
            template_name="ubuntu-vscode-hermes-desktop",
        )


def test_issuer_mint_guard_stays_honest_after_substitution() -> None:
    # Regression: the substitution pass replaces every occurrence of the marker,
    # so a guard written as `[ "$VAR" != "__MARKER__" ]` becomes `[ "$VAR" !=
    # "sk-real" ]` after replacement and rejects the real key. The mint guard must
    # compare against the never-substituted sentinel instead.
    template = Path(
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
    ).read_text(encoding="utf-8")
    # Same semantics as _rendered_template_dir: every occurrence is replaced.
    replaced = template.replace("__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY__", "sk-real-issuer")
    assert 'DOKPLOY_WIZARD_LITELLM_ISSUER_KEY="sk-real-issuer"' in replaced
    assert '!= "sk-real-issuer"' not in replaced
    # The same trap had a second shape: the pre-identity-fix FileX seed guard
    # read the issuer through a `case` list. A bare marker pattern inside that
    # list substituted to the real key and self-matched the treat-as-missing
    # branch (measured: a from-zero box derived every workspace-account
    # password from the provisioner token because the issuer could never win
    # the case). The seed derivation is gone — FileX accounts carry the stack
    # admin password, like gitea's — so no issuer `case` survives anywhere and
    # the single honest guard is the `if`-form sentinel compare above.
    assert '""|sk-real-issuer' not in replaced
    assert replaced.count('case "$${DOKPLOY_WIZARD_LITELLM_ISSUER_KEY:-}" in') == 0
    # The sentinel marker is absent from the wizard's replacement map, so it
    # stays literal in the pushed template: assert the map never grows one.
    import inspect

    source = inspect.getsource(coder_module.DokployCoderBackend.ensure_application_ready)
    assert "__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY_SENTINEL__" not in source


def _workspace_list_json(rows: list[dict[str, object]]) -> str:
    import json as _json

    return _json.dumps(rows)


def _fake_docker_cli(monkeypatch: pytest.MonkeyPatch, list_stdout: str) -> list[list[str]]:
    """Route coder list/update through a fake docker exec and record the argv."""
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if "list" in command:
            return subprocess.CompletedProcess(command, 0, stdout=list_stdout, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)
    return calls


def test_update_workspace_if_outdated_updates_matching_stale_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_docker_cli(
        monkeypatch,
        _workspace_list_json(
            [{"name": "examplecom-workspace", "owner_name": "admin", "template_name": "ubuntu-vscode", "outdated": True}]
        ),
    )
    updated = coder_module._update_workspace_if_outdated(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )
    assert updated is True
    update_calls = [c for c in calls if "update" in c]
    assert len(update_calls) == 1
    assert "admin/examplecom-workspace" in update_calls[0]
    assert "--yes" in update_calls[0]


def test_update_workspace_if_outdated_ignores_fresh_and_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_docker_cli(
        monkeypatch,
        _workspace_list_json(
            [
                {"name": "examplecom-workspace", "owner_name": "admin", "template_name": "other", "outdated": True},
            ]
        ),
    )
    updated = coder_module._update_workspace_if_outdated(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )
    assert updated is False
    assert [c for c in calls if "update" in c] == []

    calls2 = _fake_docker_cli(
        monkeypatch,
        _workspace_list_json(
            [{"name": "examplecom-workspace", "owner_name": "admin", "template_name": "ubuntu-vscode", "outdated": False}]
        ),
    )
    updated2 = coder_module._update_workspace_if_outdated(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )
    assert updated2 is False
    assert [c for c in calls2 if "update" in c] == []


def test_update_workspace_if_outdated_raises_when_update_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "list" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=_workspace_list_json(
                    [{"name": "examplecom-workspace", "owner_name": "admin", "template_name": "ubuntu-vscode", "outdated": True}]
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)
    with pytest.raises(coder_module.CoderError, match="Unable to update Coder workspace"):
        coder_module._update_workspace_if_outdated(
            container_name="wizard-stack-coder-container",
            hostname="coder.example.com",
            session_token="session-123",
            workspace_name="examplecom-workspace",
            template_name="ubuntu-vscode",
        )


def test_ensure_default_workspace_reports_updated_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: ("examplecom-workspace",),
    )
    monkeypatch.setattr(
        coder_module,
        "_update_workspace_if_outdated",
        lambda **kwargs: True,
    )
    outcome = coder_module._ensure_default_workspace(
        container_name="wizard-stack-coder-container",
        hostname="coder.example.com",
        session_token="session-123",
        workspace_name="examplecom-workspace",
        template_name="ubuntu-vscode",
    )
    assert outcome.action == "exists"
    assert outcome.updated is True


def test_ensure_application_ready_updates_workspace_on_verified_bootstrap_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The healthy box's coder pass rides the full adopt-or-create lane.

    A template push still happened above, and the running workspace keeps its
    old build until something updates it — so the convergence check inside
    `_ensure_default_workspace` must ride the verified path. The whole lane
    rides it: an upgraded box whose compose application verifies clean is
    exactly the box whose legacy date-stamped workspaces still need adopting
    (the pre-fix branch skipped the lane there and pinned those names, and
    the retired templates holding them, in place forever)."""
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
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module, "_coder_container_name", lambda service_name: "wizard-stack-coder-container"
    )
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_seed_template", lambda **kwargs: False)
    monkeypatch.setattr(coder_module, "_migrate_retired_template_workspaces", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(coder_module, "_default_workspace_name", lambda hostname: "examplecom-workspace")
    monkeypatch.setattr(
        backend, "_verify_current_compose_application", lambda: SimpleNamespace(passed=True)
    )
    monkeypatch.setattr(
        coder_module, "_list_workspaces", lambda **kwargs: ("examplecom-workspace",)
    )
    update_calls: list[str] = []

    def fake_update(**kwargs: object) -> bool:
        update_calls.append(str(kwargs["workspace_name"]))
        return True

    monkeypatch.setattr(coder_module, "_update_workspace_if_outdated", fake_update)

    notes = backend.ensure_application_ready()

    assert update_calls == ["examplecom-workspace"]
    assert any("Updated Coder workspace 'examplecom-workspace'" in note for note in notes)


def test_workspace_template_name_reads_workspace_list_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template lookup rides the list payload, not `coder show`.

    The upgraded-box lesson: Coder v2.38.3 rejects `coder show --output`, so a
    show-based read returned None for every workspace, the migrate lane
    silently skipped every legacy workspace, and the retired templates holding
    them could never be removed. The list payload is the shape every other
    coder lane already parses (flat ``owner_name``/``template_name``), so the
    lookup reads there and keeps the nested-build fallback for older shapes.
    """

    calls = _fake_docker_cli(
        monkeypatch,
        _workspace_list_json(
            [
                {
                    "name": "assignment-1",
                    "owner_name": "grader",
                    "template_name": "operator-custom",
                },
                {
                    "name": "examplecom-workspace",
                    "owner_name": "admin",
                    "latest_build": {"template_name": "ubuntu-vscode"},
                },
                {"name": "shapeless", "owner_name": "admin"},
            ]
        ),
    )
    assert (
        coder_module._workspace_template_name(
            container_name="wizard-stack-coder-container",
            hostname="coder.example.com",
            session_token="session-123",
            workspace_name="examplecom-workspace",
        )
        == "ubuntu-vscode"
    )
    # Unknown shape yields None — the adopt lane treats that conservatively
    # (wizard-created) rather than trusting a stale guess.
    assert (
        coder_module._workspace_template_name(
            container_name="wizard-stack-coder-container",
            hostname="coder.example.com",
            session_token="session-123",
            workspace_name="shapeless",
        )
        is None
    )
    for command in calls:
        assert "show" not in command
        assert "list" in command


def test_ensure_application_ready_adopts_legacy_workspaces_on_verified_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy upgraded box converges its legacy workspace names in place.

    The box this pins: compose application verifies clean (so the old branch
    skipped the ensure lane entirely), while legacy date-stamped workspaces on
    a retired template still stand. The lane now rides the verified path: the
    newest wizard-owned dated workspace is adopted via rename, the older
    duplicate pruned, and the operator's own workspace names are left alone.
    """
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
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module, "_coder_container_name", lambda service_name: "wizard-stack-coder-container"
    )
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_seed_template", lambda **kwargs: False)
    monkeypatch.setattr(coder_module, "_migrate_retired_template_workspaces", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(coder_module, "_default_workspace_name", lambda hostname: "examplecom-workspace")
    monkeypatch.setattr(
        backend, "_verify_current_compose_application", lambda: SimpleNamespace(passed=True)
    )
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: (
            "Assignment-1",
            "examplecom-workspace-2026-10-28",
            "examplecom-workspace-2026-11-03",
            "workspace2",
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_workspace_template_name",
        lambda **kwargs: coder_module._default_template_name(),
    )
    renames: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coder_module,
        "_rename_workspace",
        lambda *, workspace_name, new_workspace_name, **kwargs: renames.append(
            (workspace_name, new_workspace_name)
        ),
    )
    deletes: list[str] = []
    monkeypatch.setattr(
        coder_module,
        "_delete_workspace",
        lambda *, workspace_name, **kwargs: deletes.append(workspace_name),
    )
    monkeypatch.setattr(coder_module, "_update_workspace_if_outdated", lambda **kwargs: False)

    notes = backend.ensure_application_ready()

    assert renames == [("examplecom-workspace-2026-11-03", "examplecom-workspace")]
    assert deletes == ["examplecom-workspace-2026-10-28"]
    assert (
        "Renamed legacy default Coder workspace 'examplecom-workspace-2026-11-03' "
        "to 'examplecom-workspace'." in notes
    )
    assert (
        "Pruned duplicate wizard-created Coder workspace 'examplecom-workspace-2026-10-28'."
        in notes
    )


def test_ensure_application_ready_verified_path_survives_update_failure(
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
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module, "_coder_container_name", lambda service_name: "wizard-stack-coder-container"
    )
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_seed_template", lambda **kwargs: False)
    monkeypatch.setattr(coder_module, "_migrate_retired_template_workspaces", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_coder_roster", lambda **kwargs: {})
    monkeypatch.setattr(coder_module, "_default_workspace_name", lambda hostname: "examplecom-workspace")
    monkeypatch.setattr(
        backend, "_verify_current_compose_application", lambda: SimpleNamespace(passed=True)
    )
    monkeypatch.setattr(
        coder_module, "_list_workspaces", lambda **kwargs: ("examplecom-workspace",)
    )

    def fail_update(**kwargs: object) -> bool:
        raise coder_module.CoderError("boom")

    monkeypatch.setattr(coder_module, "_update_workspace_if_outdated", fail_update)

    notes = backend.ensure_application_ready()
    assert any("Coder default workspace step did not complete" in note for note in notes)


# ---------------------------------------------------------------------------
# Identity reconciliation: Gitea pre-flight spring, the standalone sync body,
# and the host timer that runs it between wizard passes.
# ---------------------------------------------------------------------------


def test_prestand_gitea_identities_creates_free_logins_and_spares_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Gitea pre-stand mirrors the FileX lane's rules exactly."""
    calls: list[dict[str, object]] = []

    def fake_rows(*, hostname: str, token: str) -> list[dict[str, object]]:
        del hostname, token
        return [
            {"username": "dokploy-wizard", "email": "dokploy-wizard@gitea.example.com"},
            {"username": "person", "email": "Person@Example.com"},
            {"username": "bob", "email": "bob@manual.test"},
            {"username": "legacy", "email": "legacy@coder.wizard.local"},
        ]

    def fake_request(**kwargs: object) -> tuple[int, object]:
        calls.append(dict(kwargs))
        return (201, {"id": 7})

    monkeypatch.setattr(coder_module, "_gitea_admin_user_rows", fake_rows)
    monkeypatch.setattr(coder_module, "_gitea_api_request", fake_request)

    notes, created = coder_module._prestand_gitea_identities(
        gitea_hostname="gitea.example.com",
        provisioner_token="gt-token",
        coder_users={
            "person": "person@example.com",  # email already standing
            "bob": "bob@example.com",  # manual account owns the login
            "legacy": "legacy@example.com",  # pre-fix twin owns the login
            "carol": "carol@example.com",  # free login: created
        },
        password="StackPw123",
    )

    assert notes == ["Created Gitea account 'carol' (identity pre-flight)."]
    assert created == {"carol"}
    assert calls == [
        {
            "hostname": "gitea.example.com",
            "method": "POST",
            "path": "/api/v1/admin/users",
            "token": "gt-token",
            "payload": {
                "username": "carol",
                "email": "carol@example.com",
                "password": "StackPw123",
                # A pending must-change turns every basic-auth call into a 403
                # (measured); the lane's whole point is a working login.
                "must_change_password": False,
                "send_notify_email": False,
            },
        }
    ]


def test_prestand_gitea_identities_raises_when_create_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coder_module, "_gitea_admin_user_rows", lambda **kwargs: [])
    monkeypatch.setattr(coder_module, "_gitea_api_request", lambda **kwargs: (500, None))
    with pytest.raises(coder_module.CoderError, match="HTTP 500"):
        coder_module._prestand_gitea_identities(
            gitea_hostname="gitea.example.com",
            provisioner_token="gt-token",
            coder_users={"carol": "carol@example.com"},
            password="pw",
        )


def test_prestand_gitea_identities_races_note_keeps_the_claim_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 (boot lane won the race) notes honestly and claims nothing."""
    monkeypatch.setattr(coder_module, "_gitea_admin_user_rows", lambda **kwargs: [])
    monkeypatch.setattr(coder_module, "_gitea_api_request", lambda **kwargs: (409, None))
    notes, created = coder_module._prestand_gitea_identities(
        gitea_hostname="gitea.example.com",
        provisioner_token="gt-token",
        coder_users={"carol": "carol@example.com"},
        password="pw",
    )
    assert created == set()
    assert len(notes) == 1 and notes[0].startswith("Skipped Gitea account for 'carol'")


def test_sync_gitea_identities_springs_missing_rows_and_claims_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full lane pass: re-enable claims, spring fresh logins, claim both."""
    monkeypatch.setattr(
        coder_module,
        "_gitea_admin_user_rows",
        lambda **kwargs: [
            {"username": "dokploy-wizard", "email": "prov@gitea.example.com", "active": True},
            {"username": "clayton", "email": "clayton@example.com", "active": False},
        ],
    )

    def fake_request(**kwargs: object) -> tuple[int, object]:
        return (200, {}) if kwargs["method"] == "PATCH" else (201, {"id": 9})

    monkeypatch.setattr(coder_module, "_gitea_api_request", fake_request)

    notes, claimed = coder_module._sync_gitea_identities(
        coder_hostname="coder.example.com",
        session_token="session-123",
        gitea_hostname="gitea.example.com",
        provisioner_token="gt-token",
        provisioner_username="dokploy-wizard",
        tracked={"clayton", "purged"},
        coder_users={
            "clayton": "clayton@example.com",
            "purged": "purged@example.com",  # Coder user lives on; row was deleted outright
            "newcomer": "newcomer@example.com",
        },
        password="StackPw123",
    )

    assert "Re-enabled Gitea account 'clayton' (Coder user returned)." in notes
    assert "Created Gitea account 'newcomer' (identity pre-flight)." in notes
    # The created row was not in the snapshot the claim prune ran against;
    # without the union the ledger write would drop it on this very pass.
    assert claimed == {"clayton", "purged", "newcomer"}


def test_sync_gitea_identities_without_password_does_not_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flips-only fallback stays intact for callers without a secret."""
    monkeypatch.setattr(coder_module, "_gitea_admin_user_rows", lambda **kwargs: [])
    monkeypatch.setattr(
        coder_module, "_gitea_api_request", lambda **kwargs: pytest.fail("no API calls expected")
    )
    notes, claimed = coder_module._sync_gitea_identities(
        coder_hostname="coder.example.com",
        session_token="session-123",
        gitea_hostname="gitea.example.com",
        provisioner_token="gt-token",
        provisioner_username="dokploy-wizard",
        tracked=set(),
        coder_users={"newcomer": "newcomer@example.com"},
    )
    assert notes == []
    assert claimed == set()


def test_gitea_identity_lane_persists_sprung_claims_to_state_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dokploy_wizard.state.models import STATE_FORMAT_VERSION, GiteaProvisionerState
    from dokploy_wizard.state.store import load_gitea_identities

    monkeypatch.setattr(
        coder_module,
        "_gitea_admin_user_rows",
        lambda **kwargs: [
            {"username": "dokploy-wizard", "email": "prov@gitea.example.com", "active": True}
        ],
    )
    monkeypatch.setattr(coder_module, "_gitea_api_request", lambda **kwargs: (201, {"id": 9}))

    notes = coder_module._gitea_identity_lane(
        state_dir=tmp_path,
        coder_hostname="coder.example.com",
        session_token="session-123",
        gitea_hostname="gitea.example.com",
        provisioner=GiteaProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username="dokploy-wizard",
            email="dokploy-wizard@gitea.example.com",
            password="pw",
            token="gt-token",
        ),
        coder_users={"newcomer": "newcomer@example.com"},
        password="StackPw123",
    )

    assert "Created Gitea account 'newcomer' (identity pre-flight)." in notes
    ledger = load_gitea_identities(tmp_path)
    assert ledger is not None
    assert ledger.usernames == ("newcomer",)


def test_sync_stack_identities_isolates_lanes_and_ends_the_login_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import dokploy_wizard.state.store as state_store
    from dokploy_wizard.state.models import (
        STATE_FORMAT_VERSION,
        FilexProvisionerState,
        GiteaProvisionerState,
    )

    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-9")
    monkeypatch.setattr(
        coder_module,
        "_coder_roster",
        lambda **kwargs: {"clayton": "clayton@example.com"},
    )

    def failing_gitea_lane(**kwargs: object) -> list[str]:
        raise coder_module.CoderError("gitea down")

    monkeypatch.setattr(coder_module, "_gitea_identity_lane", failing_gitea_lane)
    monkeypatch.setattr(
        coder_module,
        "_sync_filex_identities",
        lambda **kwargs: ["Created FileX account 'clayton' (identity pre-flight)."],
    )
    logged_out: list[str] = []
    monkeypatch.setattr(
        coder_module, "_coder_logout", lambda **kwargs: logged_out.append(kwargs["session_token"])
    )
    monkeypatch.setattr(
        state_store,
        "load_gitea_provisioner",
        lambda state_dir: GiteaProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username="dokploy-wizard",
            email="dokploy-wizard@gitea.example.com",
            password="pw",
            token="gt-token",
        ),
    )
    monkeypatch.setattr(
        state_store,
        "load_filex_provisioner",
        lambda state_dir: FilexProvisionerState(
            format_version=STATE_FORMAT_VERSION,
            username="dokploy-wizard",
            email="dokploy-wizard@filex.example.com",
            password="pw",
            user_id=2,
            token="fx-token",
            token_id=3,
        ),
    )

    notes = coder_module.sync_stack_identities(
        coder_hostname="coder.example.com",
        admin_email="admin@example.com",
        admin_password="pw",
        gitea_hostname="gitea.example.com",
        filex_hostname="files.example.com",
        state_dir=tmp_path,
    )

    # One surface down, the others still ran, and the pass left no login behind.
    assert any(note.startswith("Skipped Gitea identity sync:") for note in notes)
    assert "Created FileX account 'clayton' (identity pre-flight)." in notes
    assert logged_out == ["session-9"]


def test_sync_stack_identities_logs_out_when_the_roster_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-9")
    monkeypatch.setattr(
        coder_module, "_coder_roster", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    logged_out: list[str] = []
    monkeypatch.setattr(
        coder_module, "_coder_logout", lambda **kwargs: logged_out.append(kwargs["session_token"])
    )

    notes = coder_module.sync_stack_identities(
        coder_hostname="coder.example.com",
        admin_email="admin@example.com",
        admin_password="pw",
        gitea_hostname="",
        filex_hostname="",
        state_dir=tmp_path,
    )

    assert any(note.startswith("Skipped identity sync: Coder roster unreadable") for note in notes)
    assert logged_out == ["session-9"]


def test_coder_logout_prefers_post_and_falls_back_to_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Measured at the box pin (v2.39.4): POST answers 200, DELETE answers 404.
    attempts: list[str] = []

    def fake_request(**kwargs: object) -> dict[str, object]:
        attempts.append(str(kwargs["method"]))
        if kwargs["method"] == "POST":
            raise coder_module._CoderHTTPError(status=404)
        return {}

    monkeypatch.setattr(coder_module, "_coder_request", fake_request)
    coder_module._coder_logout(hostname="coder.example.com", session_token="session-9")
    assert attempts == ["POST", "DELETE"]


def test_gitea_boot_lane_password_resync_is_admin_gated() -> None:
    """The boot lane re-syncs the stack password for the deployer login only."""
    template = Path(
        "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
    ).read_text(encoding="utf-8")
    assert 'export DOKPLOY_GT_ADMIN_LOGIN="__DOKPLOY_WIZARD_LITELLM_ADMIN_LOGIN__"' in template
    assert 'admin_login = os.environ.get("DOKPLOY_GT_ADMIN_LOGIN", "").strip().lower()' in template
    assert "if login == admin_login:" in template
    assert 'note("kept the personal Gitea password' in template
    # The unconditional resync must be gone: the stomp is what stranded Mailson.
    assert (
        template.count(
            'note("synced the Gitea password for \'" + login + "\' to the stack admin password")'
        )
        == 1
    )
    gate_start = template.index("admin_login = os.environ.get")
    gate_end = template.index("kept the personal Gitea password")
    assert template.index('{"login": login, "password": password, "must_change_password": False}') > gate_start
    assert template.index('{"login": login, "password": password, "must_change_password": False}') < gate_end


def _identity_timer_backend(tmp_path: Path) -> DokployCoderBackend:
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
        state_dir=tmp_path,
        client=cast(DokployCoderApi, FakeCoderApi()),
    )


def _stub_timer_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the host-side steps of a coder apply (the fan-out test's setup)."""
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: False)
    monkeypatch.setattr(coder_module, "_create_coder_first_user", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(coder_module, "_coder_container_name", lambda service_name: "container")
    monkeypatch.setattr(coder_module, "_active_template_version_name", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_template_version_names", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_sync_hermes_workspace_secrets", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_copy_template_into_container", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(
        coder_module,
        "_ensure_default_workspace",
        lambda **kwargs: coder_module.DefaultWorkspaceOutcome(action="exists"),
    )
    monkeypatch.setattr(coder_module, "_retire_removed_templates", lambda **kwargs: ())
    monkeypatch.setattr(coder_module, "_migrate_retired_template_workspaces", lambda **kwargs: ())
    monkeypatch.setattr(
        coder_module, "_coder_roster", lambda **kwargs: {"clayton": "clayton@example.com"}
    )


def test_ensure_application_ready_records_identity_timer_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dokploy_wizard.packs.coder import coder_identity_timer_resource

    backend = _identity_timer_backend(tmp_path)
    _stub_timer_host(monkeypatch)
    monkeypatch.setattr(
        coder_module, "_install_identity_timer", lambda **kwargs: (["Identity timer note."], True)
    )

    notes = backend.ensure_application_ready()

    assert "Identity timer note." in notes
    assert backend.apply_time_owned_resources == (coder_identity_timer_resource("wizard-stack"),)


def test_ensure_application_ready_without_timer_owns_nothing_extra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # `identity_timer` (autouse) already answers ([], False) for every test.
    backend = _identity_timer_backend(tmp_path)
    _stub_timer_host(monkeypatch)

    backend.ensure_application_ready()

    assert backend.apply_time_owned_resources == ()


def test_identity_timer_unit_texts_bake_the_canonical_paths(tmp_path: Path) -> None:
    env_file = tmp_path / ".install.env"
    state_dir = tmp_path / "state"
    service, timer = coder_module._identity_timer_unit_texts(
        env_file=env_file, state_dir=state_dir
    )
    src_dir = str(Path(coder_module.__file__).resolve().parents[2])
    assert f"ConditionPathExists={env_file}" in service
    assert f"Environment=PYTHONPATH={src_dir}" in service
    assert (
        "ExecStart=/usr/bin/python3 -m dokploy_wizard sync-identities "
        in service
    )
    assert f"--env-file {env_file} --state-dir {state_dir}" in service
    assert "Type=oneshot" in service
    assert "OnBootSec=3min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "WantedBy=timers.target" in timer


def test_install_identity_timer_writes_enables_and_stays_quiet_on_rerun(
    identity_timer: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env_file = tmp_path / ".install.env"
    env_file.write_text("STACK_NAME=x\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(coder_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(coder_module, "_SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    notes, owned = identity_timer(state_dir=state_dir)  # type: ignore[operator]
    assert owned is True
    assert any("Enabled the identity reconciliation timer" in note for note in notes)
    assert (unit_dir / "dokploy-wizard-identity.service").is_file()
    assert (unit_dir / "dokploy-wizard-identity.timer").is_file()
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "dokploy-wizard-identity.timer"] in calls

    calls.clear()
    notes, owned = identity_timer(state_dir=state_dir)  # type: ignore[operator]
    assert owned is True
    # Same rendered text: no daemon-reload churn; enable stays re-affirmed.
    assert ["systemctl", "daemon-reload"] not in calls
    assert ["systemctl", "enable", "--now", "dokploy-wizard-identity.timer"] in calls

    # A vanished env file with standing units keeps the ownership claim (the
    # pair is still ours to remove); the Condition keeps the journal quiet.
    env_file.unlink()
    _notes, owned = identity_timer(state_dir=state_dir)  # type: ignore[operator]
    assert owned is True


def test_install_identity_timer_skips_without_root_or_env_file(
    identity_timer: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    monkeypatch.setattr(coder_module, "_SYSTEMD_UNIT_DIR", unit_dir)

    monkeypatch.setattr(coder_module.os, "geteuid", lambda: 1000)
    notes, owned = identity_timer(state_dir=state_dir)  # type: ignore[operator]
    assert owned is False
    assert any("not running as root" in note for note in notes)

    # An unrooted pass with the pair already on disk still reports ownership:
    # the files are ours to remove at uninstall even if this pass could not
    # refresh them (review catch: dropping the row here strands the timer).
    (unit_dir / "dokploy-wizard-identity.service").write_text("[Service]\n", encoding="utf-8")
    (unit_dir / "dokploy-wizard-identity.timer").write_text("[Timer]\n", encoding="utf-8")
    _notes, owned = identity_timer(state_dir=state_dir)  # type: ignore[operator]
    assert owned is True

    monkeypatch.setattr(coder_module.os, "geteuid", lambda: 0)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    notes, owned = identity_timer(state_dir=state_dir)  # type: ignore[operator]
    assert owned is True
    assert not any("no .install.env" in note for note in notes)

    # A write failure on a pair that already stands keeps the claim too.
    (tmp_path / ".install.env").write_text("STACK_NAME=x\n", encoding="utf-8")

    def failing_write(*args: object, **kwargs: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(coder_module.Path, "write_text", failing_write)
    _notes, owned = identity_timer(state_dir=tmp_path / "state")  # type: ignore[operator]
    assert owned is True


def test_build_coder_ledger_rebuilds_identity_timer_extras() -> None:
    from dokploy_wizard.packs.coder import coder_identity_timer_resource

    existing = OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource("coder_service", "svc-old", "stack:demo:coder:service"),
            coder_identity_timer_resource("demo"),
        ),
    )

    rebuilt = build_coder_ledger(
        existing_ledger=existing,
        stack_name="demo",
        service_resource_id="svc-new",
        data_resource_id="data-new",
        extra_resources=(coder_identity_timer_resource("demo"),),
    )

    rows = [(item.resource_type, item.resource_id) for item in rebuilt.resources]
    assert rows.count(("coder_identity_timer", "dokploy-wizard-identity.timer")) == 1
    assert ("coder_service", "svc-old") not in rows
    assert ("coder_service", "svc-new") in rows
    assert ("coder_data", "data-new") in rows

    # No extras (e.g. the coder apply ran unrooted, or a unit write failed):
    # the prior row survives the rebuild. The pair still stands on disk in
    # every such case — dropping the row there is exactly what strands an
    # enabled timer against a torn-down stack, because uninstall deletes only
    # what the ledger can name (and the executor's unconditional unit
    # teardown is the backstop of last resort).
    kept = build_coder_ledger(
        existing_ledger=existing,
        stack_name="demo",
        service_resource_id="svc-new",
        data_resource_id="data-new",
    )
    assert ("coder_identity_timer", "dokploy-wizard-identity.timer") in [
        (item.resource_type, item.resource_id) for item in kept.resources
    ]
