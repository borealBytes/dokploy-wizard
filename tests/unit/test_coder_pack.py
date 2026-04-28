# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import dokploy_wizard.dokploy.coder as coder_module
import pytest
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.coder import DokployCoderApi, DokployCoderBackend, _render_compose_file
from dokploy_wizard.packs.coder import build_coder_ledger, reconcile_coder
from dokploy_wizard.packs.coder.models import CoderResourceRecord
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


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

    def update_compose(self, *, compose_id: str, compose_file: str):
        del compose_id
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
    )

    assert 'CODER_ACCESS_URL: "https://coder.example.com/"' in compose
    assert 'CODER_WILDCARD_ACCESS_URL: "*.coder.example.com"' in compose
    assert (
        'CODER_PG_CONNECTION_URL: "postgres://wizard_stack_coder:change-me@wizard-stack-shared-postgres:5432/wizard_stack_coder?sslmode=disable"'
        in compose
    )
    assert 'CODER_PROXY_TRUSTED_HEADERS: "X-Forwarded-For"' in compose
    assert 'CODER_PROXY_TRUSTED_ORIGINS: "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"' in compose
    assert "CODER_REDIRECT_TO_ACCESS_URL:" not in compose
    assert '    user: "0:0"' in compose
    assert "      - /var/run/docker.sock:/var/run/docker.sock" in compose
    assert 'traefik.http.routers.wizard-stack-coder.rule: "Host(`coder.example.com`)"' in compose
    assert (
        'traefik.http.routers.wizard-stack-coder.middlewares: "wizard-stack-coder-forwarded-https"'
        in compose
    )
    assert (
        'traefik.http.routers.wizard-stack-coder-wildcard.rule: "HostRegexp(`{subdomain:.+}.coder.example.com`)"'
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
        'traefik.http.middlewares.wizard-stack-coder-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "coder.example.com"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-coder-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"'
        in compose
    )
    assert 'traefik.http.services.wizard-stack-coder.loadbalancer.server.port: "3000"' in compose


def test_default_coder_template_restores_workspace_bootstrap_tools() -> None:
    template = Path("templates/coder/default-ubuntu-code-server/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'apt-get install -y curl git ca-certificates wget btop' in template
    assert 'if ! command -v opencode >/dev/null 2>&1; then' in template
    assert 'if ! OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/install | bash; then' in template
    assert 'if [ ! -x /home/coder/.opencode/bin/opencode ]; then' in template
    assert 'echo "OpenCode installer did not produce a usable binary" >&2' in template
    assert 'exit 1' in template
    assert 'if [ -x /home/coder/.opencode/bin/opencode ]; then' in template
    assert 'ln -sf /home/coder/.opencode/bin/opencode /usr/local/bin/opencode' in template
    assert 'if ! command -v zellij >/dev/null 2>&1; then' in template
    assert 'zellij-$${ARCH}-unknown-linux-musl.tar.gz' in template


def test_default_opencode_web_template_includes_web_app() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-opencode-web/main.tf").read_text(
        encoding="utf-8"
    )

    assert 'apt-get install -y curl git ca-certificates wget btop' in template
    assert 'if ! command -v opencode >/dev/null 2>&1; then' in template
    assert 'if ! OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/install | bash; then' in template
    assert 'if [ ! -x /home/coder/.opencode/bin/opencode ]; then' in template
    assert 'ln -sf /home/coder/.opencode/bin/opencode /usr/local/bin/opencode' in template
    assert 'NEED_NODE=true' in template
    assert 'OPENCODE_WEB_PORT=4096' in template
    assert 'OPENCODE_PROXY_PORT=4097' in template
    assert 'nohup opencode web --hostname 127.0.0.1 --port "$OPENCODE_WEB_PORT" >/tmp/opencode-web.log 2>&1 &' in template
    assert "cat >/tmp/coder-mounted-proxy.mjs <<'JS'" in template
    assert '"accept-encoding"' in template
    assert "content-security-policy" in template
    assert "content-encoding" in template
    assert 'pageHttpOrigin + mount + next.pathname + next.search' in template
    assert 'if (input instanceof Request) return originalFetch(new Request(rewrite(input.url), input), init);' in template
    assert 'window.EventSource = class extends OriginalEventSource' in template
    assert 'window.WebSocket = class extends OriginalWebSocket' in template
    assert 'originalPushState = window.history.pushState' in template
    assert 'nohup env TARGET_PORT="$OPENCODE_WEB_PORT" PROXY_PORT="$OPENCODE_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs' in template
    assert 'resource "coder_app" "opencode"' in template
    assert 'display_name = "OpenCode"' in template
    assert 'icon         = "/icon/code.svg"' in template
    assert 'url          = "http://localhost:4097"' in template
    assert 'share        = "owner"' in template
    assert 'subdomain    = false' in template
    assert 'url       = "http://localhost:4097"' in template


def test_default_openwork_template_includes_full_webui_stack() -> None:
    template = Path(
        "templates/coder/default-ubuntu-code-server-openwork-webui/main.tf"
    ).read_text(encoding="utf-8")

    assert '$_SUDO apt-get install -y curl git ca-certificates wget btop python3' in template
    assert '$_SUDO corepack enable' in template
    assert '$_SUDO corepack prepare pnpm@10.27.0 --activate' in template
    assert '$_SUDO npm install -g openwork-orchestrator' in template
    assert 'OPENWORK_WEBUI_BUILD_KEY=v5-coder-mounted-bootstrap' in template
    assert 'OPENWORK_CLIENT_TOKEN=openwork-client-token' in template
    assert 'OPENWORK_HOST_TOKEN=openwork-host-token' in template
    assert 'git clone --depth 1 --branch dev https://github.com/different-ai/openwork "$OPENWORK_SRC_DIR"' in template
    assert 'pnpm install --frozen-lockfile' in template
    assert 'VITE_OPENWORK_DEPLOYMENT=web OPENWORK_PUBLIC_HOST=localhost VITE_ALLOWED_HOSTS=localhost,127.0.0.1 pnpm --filter @openwork/app exec vite build --base ./' in template
    assert 'perl -0pi -e ' in template
    assert 'OPENWORK_APPROVAL_MODE=auto OPENWORK_PORT=$OPENWORK_SERVER_PORT OPENWORK_TOKEN="$OPENWORK_CLIENT_TOKEN" OPENWORK_HOST_TOKEN="$OPENWORK_HOST_TOKEN" nohup openwork serve --workspace /home/coder >/tmp/openwork.log 2>&1 &' in template
    assert "pnpm exec vite preview --host 127.0.0.1 --port $OPENWORK_UI_PORT --strictPort" in template
    assert 'connection.request("GET", "/workspaces", headers={"Authorization": f"Bearer {CLIENT_TOKEN}"})' in template
    assert "cat >/tmp/openwork-webui-proxy.py <<'PY'" in template
    assert 'localStorage.setItem("openwork.server.urlOverride", baseUrl);' in template
    assert 'localStorage.setItem("openwork.server.token", token);' in template
    assert 'localStorage.setItem("openwork.server.active", baseUrl + "/opencode");' in template
    assert 'nohup python3 /tmp/openwork-webui-proxy.py >/tmp/openwork-webui-proxy.log 2>&1 &' in template
    assert 'http.server.ThreadingHTTPServer(("127.0.0.1", 8788), Proxy).serve_forever()' in template
    assert 'resource "coder_app" "openwork"' in template
    assert 'slug         = "openwork"' in template
    assert 'display_name = "OpenWork"' in template
    assert 'url          = "http://localhost:8788"' in template
    assert 'subdomain    = false' in template
    assert 'url       = "http://localhost:8788/health"' in template


def test_default_hermes_template_includes_full_web_stack() -> None:
    template = Path("templates/coder/default-ubuntu-code-server-hermes/main.tf").read_text(
        encoding="utf-8"
    )

    assert '$_SUDO apt-get install -y curl git ca-certificates wget btop python3' in template
    assert 'curl -fsSL https://deb.nodesource.com/setup_24.x | $_SUDO -E bash -' in template
    assert (
        'HERMES_HOME="$HERMES_HOME" HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup'
        in template
    )
    assert 'export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1' in template
    assert 'export HERMES_TEMPLATE_PROVIDER="__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__"' in template
    assert 'export HERMES_TEMPLATE_MODEL="__DOKPLOY_WIZARD_HERMES_MODEL__"' in template
    assert 'export HERMES_TEMPLATE_BASE_URL="__DOKPLOY_WIZARD_OPENCODE_GO_BASE_URL__"' in template
    assert 'export HERMES_TEMPLATE_API_KEY="__DOKPLOY_WIZARD_OPENCODE_GO_API_KEY__"' in template
    assert 'export HERMES_TEMPLATE_API_KEY_PLACEHOLDER="__DOKPLOY_WIZARD_OPENCODE_GO_API_KEY__"' in template
    assert 'export HERMES_INFERENCE_PROVIDER="$${HERMES_INFERENCE_PROVIDER:-$HERMES_TEMPLATE_PROVIDER}"' in template
    assert 'export HERMES_MODEL="$${HERMES_MODEL:-$HERMES_TEMPLATE_MODEL}"' in template
    assert 'export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$HERMES_TEMPLATE_BASE_URL}"' in template
    assert 'export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$HERMES_TEMPLATE_API_KEY}"' in template
    assert 'upsert_env OPENCODE_GO_API_KEY "$OPENCODE_GO_API_KEY"' in template
    assert 'API_SERVER_ENABLED=true' in template
    assert 'OPENCODE_GO_API_KEY is required for the Hermes workspace template' in template
    assert 'hermes config set model.provider "$HERMES_INFERENCE_PROVIDER"' in template
    assert 'hermes config set model.default "$HERMES_MODEL"' in template
    assert 'hermes config set model.base_url "$OPENCODE_GO_BASE_URL"' in template
    assert 'hermes config set terminal.cwd /home/coder' in template
    assert 'HERMES_BOOTSTRAP_SCRIPT=/tmp/hermes-workspace-bootstrap.sh' in template
    assert 'nohup sh "$HERMES_BOOTSTRAP_SCRIPT" >/tmp/hermes-bootstrap.log 2>&1 &' in template
    assert 'nohup hermes gateway >/tmp/hermes-gateway.log 2>&1 &' in template
    assert 'hermes dashboard --host 127.0.0.1 --port "$HERMES_DASHBOARD_PORT" --no-open' in template
    assert 'hermes-web-ui start --port "$HERMES_WEB_UI_PORT" >/tmp/hermes-web-ui-start.log 2>&1' in template
    assert 'HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=$HERMES_WEBUI_PORT HERMES_WEBUI_AGENT_DIR=$HERMES_INSTALL_DIR python3 /home/coder/.cache/hermes-webui-src/bootstrap.py --no-browser --skip-agent-install' in template
    assert 'const SYNTHETIC_HEALTHCHECK = process.env.SYNTHETIC_HEALTHCHECK === "1";' in template
    assert 'const DASHBOARD_SESSION_HEADER = process.env.DASHBOARD_SESSION_HEADER === "1";' in template
    assert 'const TOKEN_FILE = process.env.TOKEN_FILE || "";' in template
    assert 'headers["X-Hermes-Session-Token"] = await getDashboardSessionToken();' in template
    assert 'window.history.pushState = (state, title, url) => originalPushState(state, title, url == null ? url : rewrite(url));' in template
    assert "location.pathname.indexOf(\"/apps/\") !== -1" in template
    assert '.replace(/(["\'])\\/assets\\//g, "$1./assets/")' in template
    assert '.replace(/(["\'])\\/static\\//g, "$1./static/")' in template
    assert '.replace(/`\\/`\\+e/g, "`./`+e")' in template
    assert 'DASHBOARD_SESSION_HEADER=1 SYNTHETIC_HEALTHCHECK=1 TARGET_PORT=$HERMES_DASHBOARD_PORT PROXY_PORT=$HERMES_DASHBOARD_PROXY_PORT node /tmp/coder-mounted-proxy.mjs' in template
    assert 'TOKEN_FILE=/home/coder/.hermes-web-ui/.token TARGET_PORT=$HERMES_WEB_UI_PORT PROXY_PORT=$HERMES_WEB_UI_PROXY_PORT node /tmp/coder-mounted-proxy.mjs' in template
    assert 'server.on("upgrade", (req, socket, head) => {' in template
    assert 'window.WebSocket = class extends OriginalWebSocket' in template
    assert 'resource "coder_app" "hermes_dashboard"' in template
    assert 'url          = "http://localhost:9120"' in template
    assert 'resource "coder_app" "hermes_web_ui"' in template
    assert 'url          = "http://localhost:8649"' in template
    assert 'resource "coder_app" "hermes_webui"' in template
    assert 'url          = "http://localhost:8788"' in template
    assert 'HERMIES_PROVIDER' not in template
    assert 'HERMEIS_OPENCODE_GO_MODEL' not in template
    assert 'HERMIES_BASE_USL' not in template
    assert 'HERMIES_API_MODE' not in template


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
    assert phase.result.wildcard_hostname == "*.coder.example.com"
    assert phase.service_resource_id == "coder-service-1"
    assert phase.data_resource_id == "coder-data-1"
    assert phase.result.config is not None
    assert phase.result.config.wildcard_access_url == "*.coder.example.com"


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
    secret_sync_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        coder_module,
        "_sync_hermes_workspace_secrets",
        lambda **kwargs: secret_sync_calls.append(
            (
                str(kwargs["hermes_inference_provider"]),
                str(kwargs["hermes_model"]),
                str(kwargs["opencode_go_base_url"]),
            )
        ),
    )
    monkeypatch.setattr(coder_module, "_copy_template_into_container", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_push_default_template", lambda **kwargs: None)
    monkeypatch.setattr(coder_module, "_ensure_default_workspace", lambda **kwargs: False)

    notes = backend.ensure_application_ready()

    assert waits == ["coder.example.com"]
    assert secret_sync_calls == [
        ("opencode-go", "deepseek-v4-flash", "https://opencode.ai/zen/go/v1")
    ]
    assert notes == (
        "Provisioned initial Coder admin for 'admin@example.com'.",
        "Seeded default Coder template 'ubuntu-vscode'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-web'.",
        "Seeded default Coder template 'ubuntu-vscode-openwork'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes'.",
    )


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
    monkeypatch.setattr(
        coder_module,
        "_sync_hermes_workspace_secrets",
        lambda **kwargs: secret_sync_calls.append(
            (
                str(kwargs["container_name"]),
                str(kwargs["hermes_inference_provider"]),
                str(kwargs["hermes_model"]),
                kwargs["opencode_go_api_key"],
            )
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_copy_template_into_container",
        lambda *, container_name, template_dir, template_name, replacements: template_copy_calls.append(
            (container_name, str(template_dir), template_name)
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_push_default_template",
        lambda *,
        container_name,
        hostname,
        session_token,
        template_name: template_push_calls.append(
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
    assert template_copy_calls == [
        (
            "wizard-stack-coder-container",
            str(coder_module._default_template_dir()),
            coder_module._default_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            str(coder_module._default_opencode_web_template_dir()),
            coder_module._default_opencode_web_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            str(coder_module._default_openwork_template_dir()),
            coder_module._default_openwork_template_name(),
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
            coder_module._default_opencode_web_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._default_openwork_template_name(),
        ),
        (
            "wizard-stack-coder-container",
            "coder.example.com",
            "session-123",
            coder_module._default_hermes_template_name(),
        )
    ]
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
            "opencode-go",
            "deepseek-v4-flash",
            None,
        )
    ]
    assert notes == (
        "Provisioned initial Coder admin for 'clayton@openmerge.me'.",
        "Seeded default Coder template 'ubuntu-vscode'.",
        "Seeded default Coder template 'ubuntu-vscode-opencode-web'.",
        "Seeded default Coder template 'ubuntu-vscode-openwork'.",
        "Seeded default Coder template 'ubuntu-vscode-hermes'.",
        "Created default Coder workspace 'openmergeme-workspace-2026-04-18' for 'clayton@openmerge.me'.",
    )
