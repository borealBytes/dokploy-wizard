terraform {
  required_providers {
    coder = { source = "coder/coder" }
    docker = { source = "kreuzwerker/docker" }
  }
}

provider "coder" {}

variable "docker_socket" {
  type        = string
  description = "Optional docker socket URI for the Docker provider."
  default     = ""
}

provider "docker" { host = var.docker_socket != "" ? var.docker_socket : null }

data "docker_network" "shared" { name = "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__" }
data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

locals {
  username      = data.coder_workspace_owner.me.name
  runtime_image = data.coder_provisioner.me.arch == "amd64" ? "__DOKPLOY_WIZARD_RUNTIME_IMAGE_AMD64__" : "__DOKPLOY_WIZARD_RUNTIME_IMAGE_ARM64__"
  hermes_model_sync_script = <<-EOT
    set -eu
    export OPENAI_API_BASE="__DOKPLOY_WIZARD_HERMES_BASE_URL__"
    export HERMES_TEMPLATE_API_KEY="$${LITELLM_VIRTUAL_KEY_CODER_HERMES}"
    export OPENAI_API_KEY="$HERMES_TEMPLATE_API_KEY"
    export DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS="__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__/__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__"
    export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"
    exec /home/coder/.hermes/venv/bin/python /opt/dokploy-wizard/model-sync/workspace-catalog-sync.pyz --adapter hermes --workspace-root /home/coder --hermes-pid-file /home/coder/.hermes/supervisor.pid --hermes-supervisor /home/coder/.local/bin/dokploy-wizard-hermes-supervisor
  EOT
}

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"
  dir  = "/home/coder"

  startup_script = <<-EOT
    set -eu
    for runtime_command in curl git wget btop python3 opencode zellij node; do
      command -v "$runtime_command" >/dev/null 2>&1
    done

    export HERMES_HOME=/home/coder/.hermes
    export HERMES_SOURCE=/opt/dokploy-wizard/runtime/install/hermes/source
    export HERMES_CLASSIC=/opt/dokploy-wizard/runtime/install/hermes/classic
    export HERMES_PYTHON=/opt/dokploy-wizard/runtime/install/hermes/tools/python/bin/python3.11
    export HERMES_UV=/opt/dokploy-wizard/runtime/install/hermes/tools/uv/uv
    export HERMES_VENV="$HERMES_HOME/venv"
    export HERMES_TEMPLATE_PROVIDER="__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__"
    export HERMES_TEMPLATE_MODEL="__DOKPLOY_WIZARD_HERMES_MODEL__"
    export OPENAI_API_BASE="__DOKPLOY_WIZARD_HERMES_BASE_URL__"
    export HERMES_TEMPLATE_API_KEY="$${LITELLM_VIRTUAL_KEY_CODER_HERMES}"
    export OPENAI_API_KEY="$HERMES_TEMPLATE_API_KEY"
    export DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS="__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__/__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__"
    export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"
    export HERMES_DASHBOARD_PORT=9119
    export HERMES_DASHBOARD_PROXY_PORT=9120
    export HERMES_WEBUI_PORT=8787
    export HERMES_WEBUI_PROXY_PORT=8788

    test -x "$HERMES_PYTHON"
    test -x "$HERMES_UV"
    test -f "$HERMES_SOURCE/uv.lock"
    test -f "$HERMES_CLASSIC/bootstrap.py"
    "$HERMES_PYTHON" --version | grep -Fx "Python 3.11.15"
    mkdir -p "$HERMES_HOME" /home/coder/.local/bin
    upsert_env() {
      key="$1"
      value="$2"
      environment_file="$HERMES_HOME/.env"
      temporary_file=$(mktemp)
      if [ -f "$environment_file" ]; then
        grep -v "^$${key}=" "$environment_file" >"$temporary_file" || true
      fi
      printf '%s=%s\n' "$key" "$value" >>"$temporary_file"
      mv "$temporary_file" "$environment_file"
      chmod 600 "$environment_file"
    }
    upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"
    "$HERMES_UV" venv --python "$HERMES_PYTHON" "$HERMES_VENV"
    (
      cd "$HERMES_SOURCE"
      UV_PROJECT_ENVIRONMENT="$HERMES_VENV" "$HERMES_UV" sync --locked --extra all --python "$HERMES_PYTHON"
    )
    "$HERMES_VENV/bin/python" -c 'import sys, yaml; assert sys.version_info[:3] == (3, 11, 15); assert yaml.__version__ == "6.0.3"'

    "$HERMES_VENV/bin/python" - <<'PY'
import json
import os
from pathlib import Path


base_url = os.environ["OPENAI_API_BASE"].rstrip("/")
api_key = os.environ["OPENAI_API_KEY"]
model_ids = [os.environ["DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS"]]
model_ids.extend(json.loads(os.environ["DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON"]))


def copilot_models(ids: list[str]) -> dict[str, dict[str, object]]:
    return {
        model_id: {
            "name": f"Dokploy LiteLLM: {model_id}",
            "type": "chat",
            "baseUrl": base_url,
            "apiKey": api_key,
            "keyStorage": "dokploy-litellm",
            "requiresAPIKey": bool(api_key),
            "toolCalling": True,
            "vision": False,
            "thinking": False,
            "maxInputTokens": 131072,
            "maxOutputTokens": 8192,
        }
        for model_id in ids
    }


# Official Copilot BYOK is intentionally chat/agent-only.
for settings_path in (
    Path("/home/coder/.local/share/code-server/User/settings.json"),
    Path("/home/coder/.config/code-server/User/settings.json"),
):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    settings["github.copilot.chat.customOAIModels"] = copilot_models(model_ids)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
PY

    cat >/tmp/coder-mounted-proxy.mjs <<'JS'
const http = require("node:http");
const net = require("node:net");
const TARGET_PORT = Number(process.env.TARGET_PORT || "0");
const PROXY_PORT = Number(process.env.PROXY_PORT || "0");
const DASHBOARD_SESSION_HEADER = process.env.DASHBOARD_SESSION_HEADER === "1";
let dashboardSessionToken = null;
if (!TARGET_PORT || !PROXY_PORT) throw new Error("proxy ports are required");
function dashboardToken() {
  if (dashboardSessionToken) return Promise.resolve(dashboardSessionToken);
  return new Promise((resolve, reject) => {
    const request = http.request({hostname:"127.0.0.1",port:TARGET_PORT,path:"/",method:"GET"}, response => {
      const chunks=[]; response.on("data", chunk => chunks.push(Buffer.from(chunk)));
      response.on("end", () => {
        const match=Buffer.concat(chunks).toString("utf8").match(/__HERMES_SESSION_TOKEN__="([^"]+)"/);
        if (!match) { reject(new Error("dashboard session token not found")); return; }
        dashboardSessionToken=match[1]; resolve(dashboardSessionToken);
      });
    }); request.on("error", reject); request.end();
  });
}
const server = http.createServer(async (request, response) => {
  const headers={...request.headers}; delete headers.host; delete headers["accept-encoding"];
  if (DASHBOARD_SESSION_HEADER && (request.url || "").startsWith("/api/")) {
    try { headers["X-Hermes-Session-Token"] = await dashboardToken(); }
    catch (error) { response.writeHead(502); response.end("dashboard token unavailable"); return; }
  }
  const upstream=http.request({hostname:"127.0.0.1",port:TARGET_PORT,path:request.url,method:request.method,headers}, upstreamResponse => {
    response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers); upstreamResponse.pipe(response);
  });
  upstream.on("error", () => { response.writeHead(502); response.end("proxy unavailable"); }); request.pipe(upstream);
});
server.on("upgrade", (request, socket, head) => {
  const upstream=net.connect(TARGET_PORT,"127.0.0.1",() => { socket.pipe(upstream).pipe(socket); if (head.length) upstream.write(head); });
  upstream.on("error", () => socket.destroy());
});
server.listen(PROXY_PORT,"127.0.0.1");
JS

    if [ ! -f "$HERMES_HOME/config.yaml" ]; then
      "$HERMES_VENV/bin/python" /opt/dokploy-wizard/model-sync/workspace-catalog-sync.pyz --adapter hermes --workspace-root /home/coder
    fi
    # The transaction writes {"key_env": "OPENAI_API_KEY"} instead of persisting the scoped key.

    cat >/home/coder/.local/bin/dokploy-wizard-hermes-supervisor <<'SH'
#!/bin/sh
set -eu
export HERMES_HOME=/home/coder/.hermes
export HERMES_SOURCE=/opt/dokploy-wizard/runtime/install/hermes/source
export HERMES_CLASSIC=/opt/dokploy-wizard/runtime/install/hermes/classic
export HERMES_VENV="$HERMES_HOME/venv"
export HERMES_DASHBOARD_PORT=9119
export HERMES_DASHBOARD_PROXY_PORT=9120
export HERMES_WEBUI_PORT=8787
export HERMES_WEBUI_PROXY_PORT=8788
# Hermes reload keeps {"key_env": "OPENAI_API_KEY"} as an environment reference.
pid_file="$HERMES_HOME/supervisor.pid"
printf '%s\n' "$$" >"$pid_file"
cleanup() { rm -f "$pid_file"; kill "$gateway" "$dashboard" "$classic" "$dashboard_proxy" "$classic_proxy" 2>/dev/null || true; }
trap 'cleanup; exit 0' INT TERM
"$HERMES_VENV/bin/hermes" gateway >/tmp/hermes-gateway.log 2>&1 & gateway=$!
"$HERMES_VENV/bin/hermes" dashboard --host 127.0.0.1 --port "$HERMES_DASHBOARD_PORT" --no-open >/tmp/hermes-dashboard.log 2>&1 & dashboard=$!
PIP_NO_INDEX=1 HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT="$HERMES_WEBUI_PORT" HERMES_WEBUI_AGENT_DIR="$HERMES_SOURCE" HERMES_WEBUI_STATE_DIR="$HERMES_HOME/webui" HERMES_WEBUI_SKIP_ONBOARDING=1 PYTHONPATH="$HERMES_SOURCE:$HERMES_CLASSIC" "$HERMES_VENV/bin/python" "$HERMES_CLASSIC/bootstrap.py" --no-browser --skip-agent-install >/tmp/hermes-webui.log 2>&1 & classic=$!
DASHBOARD_SESSION_HEADER=1 TARGET_PORT="$HERMES_DASHBOARD_PORT" PROXY_PORT="$HERMES_DASHBOARD_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs >/tmp/hermes-dashboard-proxy.log 2>&1 & dashboard_proxy=$!
TARGET_PORT="$HERMES_WEBUI_PORT" PROXY_PORT="$HERMES_WEBUI_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs >/tmp/hermes-webui-proxy.log 2>&1 & classic_proxy=$!
wait "$gateway"
SH
    chmod 700 /home/coder/.local/bin/dokploy-wizard-hermes-supervisor
    if [ ! -f "$HERMES_HOME/supervisor.pid" ] || ! kill -0 "$(cat "$HERMES_HOME/supervisor.pid")" >/dev/null 2>&1; then
      nohup /home/coder/.local/bin/dokploy-wizard-hermes-supervisor >/tmp/hermes-supervisor.log 2>&1 &
    fi
  EOT
}

resource "coder_script" "model_sync_start" {
  agent_id           = coder_agent.main.id
  display_name       = "Refresh Hermes models"
  run_on_start       = true
  start_blocks_login = false
  script             = local.hermes_model_sync_script
}

resource "coder_script" "model_sync_periodic" {
  agent_id     = coder_agent.main.id
  display_name = "Refresh Hermes models every 15 minutes"
  cron         = "0 */15 * * * *"
  script       = local.hermes_model_sync_script
}

module "code-server" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/code-server/coder"
  version  = "1.5.2"
  agent_id = coder_agent.main.id
  folder   = "/home/coder"
  order    = 1
}

resource "coder_app" "hermes_dashboard" {
  agent_id     = coder_agent.main.id
  slug         = "hermes-dashboard"
  display_name = "Hermes Dashboard"
  icon         = format("data:image/svg+xml;base64,%s", filebase64("${path.module}/.dokploy-wizard/icons/hermes-dashboard.svg"))
  url          = "http://localhost:9120"
  share        = "owner"
  subdomain    = false
  order        = 2
  healthcheck {
    url       = "http://localhost:9120/"
    interval  = 5
    threshold = 20
  }
}

resource "coder_app" "hermes_webui" {
  agent_id     = coder_agent.main.id
  slug         = "hermes-webui"
  display_name = "Hermes WebUI Classic"
  icon         = format("data:image/svg+xml;base64,%s", trimspace(file("${path.module}/.dokploy-wizard/icons/hermes-classic.svg.b64")))
  url          = "http://localhost:8788"
  share        = "owner"
  subdomain    = false
  order        = 3
  healthcheck {
    url       = "http://localhost:8788/health"
    interval  = 5
    threshold = 20
  }
}

resource "docker_volume" "home_volume" {
  name = "coder-${data.coder_workspace.me.id}-home"
  lifecycle {
    ignore_changes = all
  }
}

resource "docker_image" "workspace" {
  name = local.runtime_image
}

resource "docker_container" "workspace" {
  count    = data.coder_workspace.me.start_count
  image    = docker_image.workspace.image_id
  name     = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname = data.coder_workspace.me.name
  entrypoint = ["sh", "-c", replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal")]
  env = [
    "CODER_AGENT_TOKEN=${coder_agent.main.token}",
    "DOKPLOY_WIZARD_CODER_CONTROL_PLANE_DATABASE_BACKEND=shared_core_postgres",
    "DOKPLOY_WIZARD_CODER_WORKSPACE_HOME_BACKEND=local_docker_volume",
    "DOKPLOY_WIZARD_CODER_WORKSPACE_HOME_STATUS=seaweedfs_deferred",
  ]
  upload {
    file           = "/opt/dokploy-wizard/model-sync/workspace-catalog-sync.pyz"
    content_base64 = filebase64("${path.module}/.dokploy-wizard/model-sync/workspace-catalog-sync.pyz")
    permissions    = "0644"
  }
  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }
  networks_advanced {
    name = data.docker_network.shared.name
  }
  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home_volume.name
    read_only      = false
  }
}
