terraform {
  required_providers {
    coder = {
      source = "coder/coder"
    }
    docker = {
      source = "kreuzwerker/docker"
    }
  }
}

provider "coder" {}

variable "docker_socket" {
  type        = string
  description = "Optional docker socket URI for the Docker provider."
  default     = ""
}

provider "docker" {
  host = var.docker_socket != "" ? var.docker_socket : null
}

data "docker_network" "shared" {
  name = "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__"
}

locals {
  username = data.coder_workspace_owner.me.name
}

# Storage boundary for this default workspace template:
# - Coder control-plane state stays on the shared-core Postgres service managed by dokploy-wizard.
# - Workspace /home stays on a per-workspace local Docker volume in this slice.
# - SeaweedFS-backed workspace/home mounting is intentionally deferred until a later task.

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"
  dir  = "/home/coder"

  startup_script = <<-EOT
    set -e

    _SUDO=""
    if command -v sudo >/dev/null 2>&1; then
      _SUDO="sudo"
    fi

    $_SUDO apt-get update -q
    $_SUDO apt-get install -y curl git ca-certificates wget btop

    # OpenCode, skip if already installed
    if ! command -v opencode >/dev/null 2>&1; then
      if ! OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/install | bash; then
        if [ ! -x /home/coder/.opencode/bin/opencode ]; then
          echo "OpenCode installer did not produce a usable binary" >&2
          exit 1
        fi
      fi
    fi

    if [ -x /home/coder/.opencode/bin/opencode ]; then
      $_SUDO ln -sf /home/coder/.opencode/bin/opencode /usr/local/bin/opencode
    fi

    # Zellij, skip if already installed
    if ! command -v zellij >/dev/null 2>&1; then
      ARCH=$(uname -m)
      ZELLIJ_URL="https://github.com/zellij-org/zellij/releases/latest/download/zellij-$${ARCH}-unknown-linux-musl.tar.gz"
      curl -fsSL "$${ZELLIJ_URL}" | $_SUDO tar -C /usr/local/bin -xz
    fi

    # Node.js, corepack, pnpm, and Pi CLI
    if ! command -v node >/dev/null 2>&1; then
      curl -fsSL https://deb.nodesource.com/setup_22.x | $_SUDO -E bash -
      $_SUDO apt-get install -y nodejs
    fi
    $_SUDO corepack enable
    $_SUDO corepack prepare pnpm@10.27.0 --activate

    export PNPM_HOME=/home/coder/.local/share/pnpm
    export PATH="$PNPM_HOME/bin:$PATH"
    mkdir -p "$PNPM_HOME/bin"
    touch /home/coder/.bashrc /home/coder/.profile
    grep -qxF "export PNPM_HOME=/home/coder/.local/share/pnpm" /home/coder/.bashrc || echo "export PNPM_HOME=/home/coder/.local/share/pnpm" >> /home/coder/.bashrc
    grep -qxF "export PATH=\"$PNPM_HOME/bin:$PATH\"" /home/coder/.bashrc || echo "export PATH=\"$PNPM_HOME/bin:$PATH\"" >> /home/coder/.bashrc
    grep -qxF "export PNPM_HOME=/home/coder/.local/share/pnpm" /home/coder/.profile || echo "export PNPM_HOME=/home/coder/.local/share/pnpm" >> /home/coder/.profile
    grep -qxF "export PATH=\"$PNPM_HOME/bin:$PATH\"" /home/coder/.profile || echo "export PATH=\"$PNPM_HOME/bin:$PATH\"" >> /home/coder/.profile

    if ! command -v pi >/dev/null 2>&1; then
      pnpm add -g @earendil-works/pi-coding-agent
    fi

    command -v pi
    pi --version
    bash -lc 'command -v pi && pi --version'

    export AI_DEFAULT_PROVIDER="$${AI_DEFAULT_PROVIDER:-__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__}"
    export AI_DEFAULT_MODEL="$${AI_DEFAULT_MODEL:-__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__}"
    export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__}"
    export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__}"
    export LITELLM_DEFAULT_ALIAS="$AI_DEFAULT_PROVIDER/$AI_DEFAULT_MODEL"
    export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"

    mkdir -p /home/coder/.pi/agent
    python3 - <<'PY'
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

base_url = os.environ["AI_DEFAULT_BASE_URL"].rstrip("/")
api_key = os.environ.get("AI_DEFAULT_API_KEY", "")
default_alias = os.environ["LITELLM_DEFAULT_ALIAS"]
fallback_models = json.loads(os.environ["DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON"])

headers = {"Accept": "application/json"}
if api_key:
    headers["Authorization"] = f"Bearer {api_key}"
request = urllib.request.Request(f"{base_url}/v1/models", headers=headers)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
except (OSError, ValueError, urllib.error.URLError):
    payload = {"data": []}

model_ids: list[str] = []
for item in payload.get("data", []):
    if not isinstance(item, dict):
        continue
    model_id = item.get("id")
    if not isinstance(model_id, str):
        continue
    normalized = model_id.strip()
    if normalized and "/" in normalized and not normalized.endswith("/*") and not normalized.startswith("openai/"):
        model_ids.append(normalized)

model_ids = list(dict.fromkeys(model_ids + fallback_models))
if default_alias not in model_ids:
    model_ids.insert(0, default_alias)

config = {
    "providers": {
        "litellm": {
            "name": "LiteLLM",
            "baseUrl": base_url,
            "api": "openai-completions",
            "apiKey": api_key,
            "models": [{"id": model_id, "name": model_id} for model_id in model_ids],
        }
    }
}
Path("/home/coder/.pi/agent/models.json").write_text(
    json.dumps(config, indent=2) + "\n",
    encoding="utf-8",
)
# Official Copilot BYOK is intentionally chat/agent-only; inline completions stay on Copilot-managed models.
def _copilot_byok_openai_base_url(raw_base_url: str) -> str:
    normalized = raw_base_url.rstrip("/")
    if normalized.endswith("/v1") or normalized.endswith("/v1/chat/completions"):
        return normalized
    return f"{normalized}/v1"


def _copilot_byok_custom_models(raw_base_url: str, raw_api_key: str, ids: list[str]) -> dict[str, dict[str, object]]:
    url = _copilot_byok_openai_base_url(raw_base_url)
    return {
        model_id: {
            "name": f"Dokploy LiteLLM: {model_id}",
            "model": model_id,
            "url": url,
            "apiKey": raw_api_key,
            "keyStorage": "dokploy-litellm",
            "requiresAPIKey": bool(raw_api_key),
            "toolCalling": True,
            "vision": False,
            "thinking": False,
            "maxInputTokens": 131072,
            "maxOutputTokens": 8192,
        }
        for model_id in ids
    }


def write_vscode_copilot_byok_settings(raw_base_url: str, raw_api_key: str, ids: list[str]) -> None:
    settings_paths = [
        Path("/home/coder/.local/share/code-server/User/settings.json"),
        Path("/home/coder/.config/code-server/User/settings.json"),
    ]
    custom_models = _copilot_byok_custom_models(raw_base_url, raw_api_key, ids)
    for settings_path in settings_paths:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        except (OSError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        settings["github.copilot.chat.customOAIModels"] = custom_models
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


write_vscode_copilot_byok_settings(base_url, api_key, model_ids)

PY

    PI_WEB_SRC_DIR=/home/coder/.cache/pi-web-ui
    PI_WEB_BUILD_STAMP=/home/coder/.cache/pi-web-ui-build-rev
    PI_WEB_BUILD_KEY=v1-coder-mounted-preview
    PI_WEB_UI_PORT=8650
    PI_WEB_PROXY_PORT=8651

    # Pi Web UI stays browser-local, but the workspace now pre-seeds custom LiteLLM
    # models with full alias IDs so Pi sends the exact /v1/models values back to the proxy.

    mkdir -p "$PI_WEB_SRC_DIR/src" /home/coder/.cache

    cat >"$PI_WEB_SRC_DIR/package.json" <<'JSON'
{
  "name": "pi-web-ui-coder",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "vite build --base ./",
    "preview": "vite preview --host 127.0.0.1 --port 8650 --strictPort"
  },
  "dependencies": {
    "@earendil-works/pi-agent-core": "^0.74.0",
    "@earendil-works/pi-ai": "^0.74.0",
    "@earendil-works/pi-web-ui": "^0.74.0"
  },
  "devDependencies": {
    "typescript": "^5.7.3",
    "vite": "^7.1.6"
  }
}
JSON

    cat >"$PI_WEB_SRC_DIR/tsconfig.json" <<'JSON'
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
JSON

    cat >"$PI_WEB_SRC_DIR/index.html" <<'HTML'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Pi Web UI</title>
    <style>
      html, body, #app {
        margin: 0;
        min-height: 100%;
      }

      body {
        font-family: Inter, ui-sans-serif, system-ui, sans-serif;
        background: #0f172a;
        color: #e2e8f0;
      }

      .pi-web-shell {
        min-height: 100vh;
        display: flex;
        flex-direction: column;
      }

      .pi-web-header {
        padding: 20px 24px 12px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.2);
        background: rgba(15, 23, 42, 0.92);
      }

      .pi-web-header h1 {
        margin: 0 0 6px;
        font-size: 1.25rem;
      }

      .pi-web-header p {
        margin: 0;
        color: #94a3b8;
        font-size: 0.95rem;
      }

      #pi-web-chat {
        flex: 1;
        min-height: 0;
      }

      #pi-web-chat > * {
        display: block;
        height: 100%;
      }
    </style>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="./src/main.ts"></script>
  </body>
</html>
HTML

    cat >"$PI_WEB_SRC_DIR/src/main.ts" <<'TS'
import { Agent } from '@earendil-works/pi-agent-core';
import { getModel } from '@earendil-works/pi-ai';
import {
  ApiKeyPromptDialog,
  AppStorage,
  ChatPanel,
  IndexedDBStorageBackend,
  ProviderKeysStore,
  SessionsStore,
  SettingsStore,
  defaultConvertToLlm,
  setAppStorage,
} from '@earendil-works/pi-web-ui';
import '@earendil-works/pi-web-ui/app.css';

const settings = new SettingsStore();
const providerKeys = new ProviderKeysStore();
const sessions = new SessionsStore();

const backend = new IndexedDBStorageBackend({
  dbName: 'pi-web-ui-coder',
  version: 1,
  stores: [
    settings.getConfig(),
    providerKeys.getConfig(),
    sessions.getConfig(),
    SessionsStore.getMetadataConfig(),
  ],
});

settings.setBackend(backend);
providerKeys.setBackend(backend);
sessions.setBackend(backend);

setAppStorage(new AppStorage(settings, providerKeys, sessions, undefined, backend));

async function init() {
  document.title = "Pi Web UI";

  const root = document.getElementById('app');
  if (!root) {
    throw new Error('Pi Web UI root not found');
  }

  root.innerHTML = `
    <div class="pi-web-shell">
      <div class="pi-web-header">
        <h1>Pi Web UI</h1>
        <p>Browser-local sessions and provider keys stay in IndexedDB for this workspace.</p>
      </div>
      <div id="pi-web-chat"></div>
    </div>
  `;

  const chatHost = document.getElementById('pi-web-chat');
  if (!chatHost) {
    throw new Error('Pi Web UI chat host not found');
  }

  const agent = new Agent({
    initialState: {
      systemPrompt: 'You are Pi, a helpful coding assistant running entirely in the browser UI.',
      model: getModel('anthropic', 'claude-sonnet-4-5-20250929'),
      thinkingLevel: 'off',
      messages: [],
      tools: [],
    },
    convertToLlm: defaultConvertToLlm,
  });

  const chatPanel = new ChatPanel();
  await chatPanel.setAgent(agent, {
    onApiKeyRequired: async (provider: string) => ApiKeyPromptDialog.prompt(provider),
  });

  chatHost.appendChild(chatPanel);
}

void init();
TS

    if [ ! -d "$PI_WEB_SRC_DIR/node_modules" ] || [ ! -f "$PI_WEB_SRC_DIR/dist/index.html" ] || [ ! -f "$PI_WEB_BUILD_STAMP" ] || [ "$(cat "$PI_WEB_BUILD_STAMP" 2>/dev/null || true)" != "$PI_WEB_BUILD_KEY" ]; then
      cd "$PI_WEB_SRC_DIR"
      CI=true pnpm install
      pnpm exec vite build --base ./
      printf '%s' "$PI_WEB_BUILD_KEY" > "$PI_WEB_BUILD_STAMP"
    fi

    nohup sh -lc "cd '$PI_WEB_SRC_DIR' && pnpm exec vite preview --host 127.0.0.1 --port $PI_WEB_UI_PORT --strictPort" >/tmp/pi-web-ui.log 2>&1 &

    cat >/tmp/coder-mounted-proxy.mjs <<'JS'
import http from "node:http";

const TARGET_HOST = process.env.TARGET_HOST || "127.0.0.1";
const TARGET_PORT = Number(process.env.TARGET_PORT || "0");
const PROXY_PORT = Number(process.env.PROXY_PORT || "0");
const SYNTHETIC_HEALTHCHECK = process.env.SYNTHETIC_HEALTHCHECK === "1";

if (!TARGET_PORT || !PROXY_PORT) {
  throw new Error("TARGET_PORT and PROXY_PORT are required");
}

function splitMountPath(pathname) {
  const idx = pathname.indexOf("/apps/");
  if (idx === -1) {
    return { mount: "", remainder: pathname || "/" };
  }
  const afterPrefix = pathname.slice(idx + "/apps/".length);
  const slug = afterPrefix.split("/")[0] || "";
  const mount = pathname.slice(0, idx + "/apps/".length + slug.length);
  const rest = pathname.slice(mount.length);
  return { mount, remainder: rest === "" ? "/" : rest };
}

function needsSpaFallback(pathname) {
  if (!pathname || pathname === "/") return true;
  const lastSegment = pathname.split("/").pop() || "";
  return !lastSegment.includes(".");
}

function rewriteLocation(locationHeader, mount) {
  if (!locationHeader) return locationHeader;
  if (locationHeader.startsWith("/")) {
    return mount + locationHeader;
  }
  return locationHeader;
}

function filteredHeaders(headers, mount) {
  const next = {};
  for (const [key, value] of Object.entries(headers)) {
    if (value == null) continue;
    const lowered = key.toLowerCase();
    if (["content-encoding", "transfer-encoding", "connection"].includes(lowered)) continue;
    next[key] = lowered === "location" ? rewriteLocation(String(value), mount) : value;
  }
  return next;
}

const server = http.createServer((req, res) => {
  const parsed = new URL(req.url || "/", "http://localhost");
  if (SYNTHETIC_HEALTHCHECK && parsed.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": "15" });
    res.end('{"status":"ok"}');
    return;
  }

  const { mount, remainder } = splitMountPath(parsed.pathname);
  const targetPath = needsSpaFallback(remainder) ? "/" : remainder + parsed.search;
  const headers = { ...req.headers };
  delete headers.host;
  delete headers["accept-encoding"];

  const upstream = http.request(
    {
      hostname: TARGET_HOST,
      port: TARGET_PORT,
      path: targetPath,
      method: req.method,
      headers,
    },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, filteredHeaders(upstreamRes.headers, mount));
      upstreamRes.pipe(res);
    },
  );

  upstream.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Proxy error: " + error.message);
  });

  req.pipe(upstream);
});

server.listen(PROXY_PORT, "127.0.0.1");
JS

    nohup env SYNTHETIC_HEALTHCHECK=1 TARGET_PORT="$PI_WEB_UI_PORT" PROXY_PORT="$PI_WEB_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs >/tmp/pi-web-ui-proxy.log 2>&1 &
  EOT
}

module "code-server" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/code-server/coder"
  version  = "~> 1.0"
  agent_id = coder_agent.main.id
  folder   = "/home/coder"
  order    = 1
}

resource "coder_app" "pi_web" {
  agent_id     = coder_agent.main.id
  slug         = "pi-web"
  display_name = "Pi Web UI"
  url          = "http://localhost:8651"
  share        = "owner"
  subdomain    = false
  order        = 2

  healthcheck {
    url       = "http://localhost:8651/health"
    interval  = 5
    threshold = 12
  }
}

resource "docker_volume" "home_volume" {
  name = "coder-${data.coder_workspace.me.id}-home"
  lifecycle {
    ignore_changes = all
  }
}

resource "docker_container" "workspace" {
  count    = data.coder_workspace.me.start_count
  image    = "codercom/enterprise-base:ubuntu"
  name     = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname = data.coder_workspace.me.name

  entrypoint = [
    "sh",
    "-c",
    replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal"),
  ]

  env = [
    "CODER_AGENT_TOKEN=${coder_agent.main.token}",
    "DOKPLOY_WIZARD_CODER_CONTROL_PLANE_DATABASE_BACKEND=shared_core_postgres",
    "DOKPLOY_WIZARD_CODER_WORKSPACE_HOME_BACKEND=local_docker_volume",
    "DOKPLOY_WIZARD_CODER_WORKSPACE_HOME_STATUS=seaweedfs_deferred",
  ]

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
