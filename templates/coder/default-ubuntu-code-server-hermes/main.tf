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
    $_SUDO apt-get install -y curl git ca-certificates wget btop python3

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

    NEED_NODE=true
    if command -v node >/dev/null 2>&1; then
      NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
      if [ "$NODE_MAJOR" -ge 24 ]; then
        NEED_NODE=false
      fi
    fi
    if [ "$NEED_NODE" = true ]; then
      curl -fsSL https://deb.nodesource.com/setup_24.x | $_SUDO -E bash -
      $_SUDO apt-get install -y nodejs
    fi

    export HERMES_HOME=/home/coder/.hermes
    export HERMES_INSTALL_DIR=/home/coder/.hermes/hermes-agent
    export NPM_CONFIG_PREFIX=/home/coder/.local
    export PATH="/home/coder/.local/bin:$PATH"
    export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
    export HERMES_TEMPLATE_PROVIDER="__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__"
    export HERMES_TEMPLATE_MODEL="__DOKPLOY_WIZARD_HERMES_MODEL__"
    export HERMES_TEMPLATE_BASE_URL="__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__"
    export HERMES_TEMPLATE_API_KEY="__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__"
    export HERMES_TEMPLATE_API_KEY_PLACEHOLDER="__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__"

    if ! command -v hermes >/dev/null 2>&1; then
      HERMES_HOME="$HERMES_HOME" HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup
    fi

    export PATH="/home/coder/.local/bin:$PATH"
    if ! command -v hermes >/dev/null 2>&1; then
      echo "Hermes installer did not produce a usable binary" >&2
      exit 1
    fi

    mkdir -p "$HERMES_HOME" /home/coder/.cache /home/coder/.local/bin

    export HERMES_INFERENCE_PROVIDER="$${HERMES_INFERENCE_PROVIDER:-$HERMES_TEMPLATE_PROVIDER}"
    export HERMES_MODEL="$${HERMES_MODEL:-$HERMES_TEMPLATE_MODEL}"
    export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-$HERMES_TEMPLATE_BASE_URL}"
    export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-$HERMES_TEMPLATE_API_KEY}"
    export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$AI_DEFAULT_BASE_URL}"
    export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$AI_DEFAULT_API_KEY}"
    export API_SERVER_ENABLED=true
    export API_SERVER_HOST=127.0.0.1
    export API_SERVER_PORT=8642
    export API_SERVER_KEY="$${API_SERVER_KEY:-hermes-local-api-key}"

    upsert_env() {
      key="$1"
      value="$2"
      file="$HERMES_HOME/.env"
      tmp_file=$(mktemp)
      if [ -f "$file" ]; then
        grep -v "^$${key}=" "$file" > "$tmp_file" || true
      fi
      printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
      mv "$tmp_file" "$file"
      chmod 600 "$file"
    }

    upsert_env AI_DEFAULT_API_KEY "$AI_DEFAULT_API_KEY"
    upsert_env OPENCODE_GO_API_KEY "$OPENCODE_GO_API_KEY"
    upsert_env HERMES_INFERENCE_PROVIDER "$HERMES_INFERENCE_PROVIDER"
    upsert_env HERMES_MODEL "$HERMES_MODEL"
    upsert_env AI_DEFAULT_BASE_URL "$AI_DEFAULT_BASE_URL"
    upsert_env OPENCODE_GO_BASE_URL "$OPENCODE_GO_BASE_URL"
    upsert_env API_SERVER_ENABLED "$API_SERVER_ENABLED"
    upsert_env API_SERVER_HOST "$API_SERVER_HOST"
    upsert_env API_SERVER_PORT "$API_SERVER_PORT"
    upsert_env API_SERVER_KEY "$API_SERVER_KEY"

    if [ "$HERMES_TEMPLATE_API_KEY" = "$HERMES_TEMPLATE_API_KEY_PLACEHOLDER" ] && [ -z "$${AI_DEFAULT_API_KEY:-}" ]; then
      echo "AI_DEFAULT_API_KEY is required for the Hermes workspace template" >&2
      exit 1
    fi
    if [ -z "$AI_DEFAULT_API_KEY" ]; then
      echo "AI_DEFAULT_API_KEY is required for the Hermes workspace template" >&2
      exit 1
    fi
    if [ "$HERMES_INFERENCE_PROVIDER" = "opencode-go" ] && [ -z "$OPENCODE_GO_API_KEY" ]; then
      echo "Provider 'opencode-go' requires OPENCODE_GO_API_KEY in the Hermes workspace template" >&2
      exit 1
    fi

    hermes config set model.provider "$HERMES_INFERENCE_PROVIDER"
    hermes config set model.default "$HERMES_MODEL"
    hermes config set model.base_url "$AI_DEFAULT_BASE_URL"
    hermes config set terminal.backend local
    hermes config set terminal.cwd /home/coder

    if ! command -v hermes-web-ui >/dev/null 2>&1; then
      npm install -g hermes-web-ui
    fi

    NESQ_DIR=/home/coder/.cache/hermes-webui-src
    if [ ! -d "$NESQ_DIR/.git" ]; then
      rm -rf "$NESQ_DIR"
      git clone --depth 1 --branch master https://github.com/nesquena/hermes-webui "$NESQ_DIR"
    else
      git -C "$NESQ_DIR" fetch --depth 1 origin master
      git -C "$NESQ_DIR" checkout -f master
      git -C "$NESQ_DIR" reset --hard origin/master
    fi

    cat >/tmp/coder-mounted-proxy.mjs <<'JS'
import http from "node:http";
import net from "node:net";
import fs from "node:fs";

const TARGET_HOST = process.env.TARGET_HOST || "127.0.0.1";
const TARGET_PORT = Number(process.env.TARGET_PORT || "0");
const PROXY_PORT = Number(process.env.PROXY_PORT || "0");
const SYNTHETIC_HEALTHCHECK = process.env.SYNTHETIC_HEALTHCHECK === "1";
const DASHBOARD_SESSION_HEADER = process.env.DASHBOARD_SESSION_HEADER === "1";
const TOKEN_FILE = process.env.TOKEN_FILE || "";
let dashboardSessionTokenCache = null;

if (!TARGET_PORT || !PROXY_PORT) {
  throw new Error("TARGET_PORT and PROXY_PORT are required");
}

function rewriteHtml(html) {
  let tokenBootstrap = "";
  if (TOKEN_FILE) {
    try {
      const token = fs.readFileSync(TOKEN_FILE, "utf-8").trim();
      if (token) {
        tokenBootstrap = `<script>
(() => {
  const token = "TOKEN_PLACEHOLDER";
  const hash = location.hash || "";
  if (!hash.includes("token=")) {
    location.replace(location.pathname + location.search + "#/?token=" + encodeURIComponent(token));
  }
})();
</script>`;
        tokenBootstrap = tokenBootstrap.replace("TOKEN_PLACEHOLDER", token);
      }
    } catch {
      tokenBootstrap = "";
    }
  }
  const mountScript = `<script>
(() => {
  let mount = location.pathname.endsWith("/") ? location.pathname.slice(0, -1) : location.pathname;
  if (location.pathname.indexOf("/apps/") !== -1) {
    const prefixLen = "/apps/".length;
    const idx = location.pathname.indexOf("/apps/");
    const afterPrefix = location.pathname.substring(idx + prefixLen);
    const appSlug = afterPrefix.split("/")[0];
    const trailing = appSlug.length > 0 ? appSlug.length : 0;
    mount = location.pathname.substring(0, idx + prefixLen + trailing);
  }
  const pageHttpOrigin = location.origin;
  const pageWsOrigin = pageHttpOrigin.replace(/^http/, "ws");
  const localHosts = new Set(["127.0.0.1", "localhost"]);
  const rewrite = (value) => {
    const raw = value instanceof URL ? value.toString() : value;
    if (typeof raw !== "string" || raw === "") return value;
    if (raw.startsWith(pageHttpOrigin + mount + "/") || raw.startsWith(pageWsOrigin + mount + "/")) return raw;
    if (raw.startsWith(pageHttpOrigin + "/")) {
      const next = new URL(raw);
      return pageHttpOrigin + mount + next.pathname + next.search + next.hash;
    }
    if (raw.startsWith(pageWsOrigin + "/")) {
      const next = new URL(raw.replace(/^ws/, "http"));
      return pageWsOrigin + mount + next.pathname + next.search + next.hash;
    }
    if (raw.startsWith("http://") || raw.startsWith("https://") || raw.startsWith("ws://") || raw.startsWith("wss://")) {
      const next = new URL(raw.replace(/^ws/, "http"));
      if (localHosts.has(next.hostname) || next.hostname === location.hostname) {
        const origin = raw.startsWith("ws") ? pageWsOrigin : pageHttpOrigin;
        return origin + mount + next.pathname + next.search + next.hash;
      }
      return raw;
    }
    if (raw.startsWith("/") && !raw.startsWith("//")) return mount + raw;
    return raw;
  };
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (input instanceof Request) return originalFetch(new Request(rewrite(input.url), input), init);
    return originalFetch(rewrite(input), init);
  };
  const OriginalEventSource = window.EventSource;
  window.EventSource = class extends OriginalEventSource {
    constructor(url, config) { super(rewrite(url), config); }
  };
  const OriginalWebSocket = window.WebSocket;
  window.WebSocket = class extends OriginalWebSocket {
    constructor(url, protocols) { super(rewrite(url), protocols); }
  };
  const originalOpen = window.XMLHttpRequest.prototype.open;
  window.XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    return originalOpen.call(this, method, rewrite(url), ...rest);
  };
  const originalPushState = window.history.pushState.bind(window.history);
  window.history.pushState = (state, title, url) => originalPushState(state, title, url == null ? url : rewrite(url));
  const originalReplaceState = window.history.replaceState.bind(window.history);
  window.history.replaceState = (state, title, url) => originalReplaceState(state, title, url == null ? url : rewrite(url));
  const originalOpenWindow = window.open.bind(window);
  window.open = (url, ...rest) => originalOpenWindow(url == null ? url : rewrite(url), ...rest);
})();
</script>`;
  return html
    .replace(/(href|src|action|content)="\//g, '$1="./')
    .replace(/<base href="\/"\s*\/>/g, '<base href="./" />')
    .replace("</head>", tokenBootstrap + mountScript + "</head>");
}

function rewriteTextPayload(text, contentType) {
  if (contentType.includes("text/html")) {
    return rewriteHtml(text);
  }
  return text
    .replace(/(["'])\/assets\//g, "$1./assets/")
    .replace(/(["'])\/static\//g, "$1./static/")
    .replace(/url\(\/assets\//g, "url(./assets/")
    .replace(/url\(\/static\//g, "url(./static/")
    .replace(/`\/`\+e/g, "`./`+e");
}

function rewriteLocation(locationHeader) {
  if (!locationHeader) return locationHeader;
  if (locationHeader.startsWith("/")) return `.$${locationHeader}`;
  if (locationHeader.startsWith("http://") || locationHeader.startsWith("https://")) {
    const next = new URL(locationHeader);
    if (next.hostname === TARGET_HOST || next.hostname === "127.0.0.1" || next.hostname === "localhost") {
      return `.$${next.pathname}$${next.search}$${next.hash}`;
    }
  }
  return locationHeader;
}

function fetchDashboardSessionToken() {
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        hostname: TARGET_HOST,
        port: TARGET_PORT,
        path: "/",
        method: "GET",
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
        response.on("end", () => {
          const html = Buffer.concat(chunks).toString("utf-8");
          const match = html.match(/__HERMES_SESSION_TOKEN__=\"([^\"]+)\"/);
          if (!match) {
            reject(new Error("dashboard session token not found"));
            return;
          }
          dashboardSessionTokenCache = match[1];
          resolve(dashboardSessionTokenCache);
        });
      },
    );
    request.on("error", reject);
    request.end();
  });
}

async function getDashboardSessionToken() {
  if (dashboardSessionTokenCache) return dashboardSessionTokenCache;
  return fetchDashboardSessionToken();
}

function filteredHeaders(headers, isHtml) {
  const next = {};
  for (const [key, value] of Object.entries(headers)) {
    if (value == null) continue;
    const lowered = key.toLowerCase();
    if (["content-security-policy", "content-encoding", "transfer-encoding", "connection"].includes(lowered)) continue;
    if (isHtml && lowered === "content-length") continue;
    next[key] = lowered === "location" ? rewriteLocation(String(value)) : value;
  }
  return next;
}

const server = http.createServer(async (req, res) => {
  if (SYNTHETIC_HEALTHCHECK && (req.url || "/") === "/health") {
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": "15" });
    res.end('{"status":"ok"}');
    return;
  }
  const headers = { ...req.headers };
  delete headers.host;
  delete headers["accept-encoding"];
  if (DASHBOARD_SESSION_HEADER && (req.url || "").startsWith("/api/")) {
    try {
      headers["X-Hermes-Session-Token"] = await getDashboardSessionToken();
    } catch (error) {
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end(`Proxy error: $${error instanceof Error ? error.message : String(error)}`);
      return;
    }
  }
  const upstream = http.request(
    {
      hostname: TARGET_HOST,
      port: TARGET_PORT,
      path: req.url,
      method: req.method,
      headers,
    },
    (upstreamRes) => {
      const contentType = String(upstreamRes.headers["content-type"] || "").toLowerCase();
      const isRewrittenText = contentType.includes("text/html") || contentType.includes("javascript") || contentType.includes("ecmascript") || contentType.includes("text/css");
      if (!isRewrittenText) {
        res.writeHead(upstreamRes.statusCode || 502, filteredHeaders(upstreamRes.headers, false));
        upstreamRes.pipe(res);
        return;
      }
      const chunks = [];
      upstreamRes.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
      upstreamRes.on("end", () => {
        const text = rewriteTextPayload(Buffer.concat(chunks).toString("utf-8"), contentType);
        const payload = Buffer.from(text, "utf-8");
        const responseHeaders = filteredHeaders(upstreamRes.headers, true);
        responseHeaders["Content-Length"] = String(payload.length);
        res.writeHead(upstreamRes.statusCode || 200, responseHeaders);
        res.end(payload);
      });
    },
  );
  upstream.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end(`Proxy error: $${error.message}`);
  });
  req.pipe(upstream);
});

server.on("upgrade", (req, socket, head) => {
  const upstream = net.connect(TARGET_PORT, TARGET_HOST, () => {
    const headerLines = [];
    headerLines.push(`GET $${req.url || "/"} HTTP/$${req.httpVersion}`);
    for (const [key, value] of Object.entries(req.headers)) {
      if (value == null) continue;
      if (key.toLowerCase() === "host") {
        headerLines.push(`Host: $${TARGET_HOST}:$${TARGET_PORT}`);
        continue;
      }
      headerLines.push(`$${key}: $${Array.isArray(value) ? value.join(", ") : value}`);
    }
    headerLines.push("\r\n");
    upstream.write(headerLines.join("\r\n"));
    if (head.length) upstream.write(head);
    socket.pipe(upstream).pipe(socket);
  });
  upstream.on("error", () => socket.destroy());
});

server.listen(PROXY_PORT, "127.0.0.1");
JS

    export HERMES_DASHBOARD_PORT=9119
    export HERMES_DASHBOARD_PROXY_PORT=9120
    export HERMES_WEB_UI_PORT=8648
    export HERMES_WEB_UI_PROXY_PORT=8649
    export HERMES_WEBUI_PORT=8787
    export HERMES_WEBUI_PROXY_PORT=8788
    HERMES_BOOTSTRAP_SCRIPT=/tmp/hermes-workspace-bootstrap.sh
    HERMES_BOOTSTRAP_PID_FILE=/tmp/hermes-workspace-bootstrap.pid

    cat >"$HERMES_BOOTSTRAP_SCRIPT" <<'SH'
set -e

wait_for_http() {
  url="$1"
  attempts="$2"
  while [ "$attempts" -gt 0 ]; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    attempts=$((attempts - 1))
    sleep 1
  done
  return 1
}

if ! command -v hermes >/dev/null 2>&1; then
  HERMES_HOME="$HERMES_HOME" HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup
fi

export PATH="/home/coder/.local/bin:$PATH"
if ! command -v hermes >/dev/null 2>&1; then
  echo "Hermes installer did not produce a usable binary" >&2
  exit 1
fi

mkdir -p "$HERMES_HOME" /home/coder/.cache /home/coder/.local/bin

upsert_env() {
  key="$1"
  value="$2"
  file="$HERMES_HOME/.env"
  tmp_file=$(mktemp)
  if [ -f "$file" ]; then
    grep -v "^$${key}=" "$file" > "$tmp_file" || true
  fi
  printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
  mv "$tmp_file" "$file"
  chmod 600 "$file"
}

upsert_env AI_DEFAULT_API_KEY "$AI_DEFAULT_API_KEY"
upsert_env OPENCODE_GO_API_KEY "$OPENCODE_GO_API_KEY"
upsert_env HERMES_INFERENCE_PROVIDER "$HERMES_INFERENCE_PROVIDER"
upsert_env HERMES_MODEL "$HERMES_MODEL"
upsert_env AI_DEFAULT_BASE_URL "$AI_DEFAULT_BASE_URL"
upsert_env OPENCODE_GO_BASE_URL "$OPENCODE_GO_BASE_URL"
upsert_env API_SERVER_ENABLED "$API_SERVER_ENABLED"
upsert_env API_SERVER_HOST "$API_SERVER_HOST"
upsert_env API_SERVER_PORT "$API_SERVER_PORT"
upsert_env API_SERVER_KEY "$API_SERVER_KEY"

if [ "$HERMES_TEMPLATE_API_KEY" = "$HERMES_TEMPLATE_API_KEY_PLACEHOLDER" ] && [ -z "$${AI_DEFAULT_API_KEY:-}" ]; then
  echo "AI_DEFAULT_API_KEY is required for the Hermes workspace template" >&2
  exit 1
fi
if [ -z "$AI_DEFAULT_API_KEY" ]; then
  echo "AI_DEFAULT_API_KEY is required for the Hermes workspace template" >&2
  exit 1
fi
if [ "$HERMES_INFERENCE_PROVIDER" = "opencode-go" ] && [ -z "$OPENCODE_GO_API_KEY" ]; then
  echo "Provider 'opencode-go' requires OPENCODE_GO_API_KEY in the Hermes workspace template" >&2
  exit 1
fi

hermes config set model.provider "$HERMES_INFERENCE_PROVIDER"
hermes config set model.default "$HERMES_MODEL"
hermes config set model.base_url "$AI_DEFAULT_BASE_URL"
hermes config set terminal.backend local
hermes config set terminal.cwd /home/coder

if ! command -v hermes-web-ui >/dev/null 2>&1; then
  npm install -g hermes-web-ui
fi

NESQ_DIR=/home/coder/.cache/hermes-webui-src
if [ ! -d "$NESQ_DIR/.git" ]; then
  rm -rf "$NESQ_DIR"
  git clone --depth 1 --branch master https://github.com/nesquena/hermes-webui "$NESQ_DIR"
else
  git -C "$NESQ_DIR" fetch --depth 1 origin master
  git -C "$NESQ_DIR" checkout -f master
  git -C "$NESQ_DIR" reset --hard origin/master
fi

if ! wait_for_http "http://127.0.0.1:$API_SERVER_PORT/health" 1; then
  nohup hermes gateway >/tmp/hermes-gateway.log 2>&1 &
  wait_for_http "http://127.0.0.1:$API_SERVER_PORT/health" 300 || true
fi

if ! wait_for_http "http://127.0.0.1:$HERMES_DASHBOARD_PORT/" 1; then
  nohup hermes dashboard --host 127.0.0.1 --port "$HERMES_DASHBOARD_PORT" --no-open >/tmp/hermes-dashboard.log 2>&1 &
  wait_for_http "http://127.0.0.1:$HERMES_DASHBOARD_PORT/" 300 || true
fi

hermes-web-ui stop >/dev/null 2>&1 || true
hermes-web-ui start --port "$HERMES_WEB_UI_PORT" >/tmp/hermes-web-ui-start.log 2>&1 || true
wait_for_http "http://127.0.0.1:$HERMES_WEB_UI_PORT/health" 300 || true

if ! wait_for_http "http://127.0.0.1:$HERMES_WEBUI_PORT/health" 1; then
  nohup env HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=$HERMES_WEBUI_PORT HERMES_WEBUI_AGENT_DIR=$HERMES_INSTALL_DIR python3 /home/coder/.cache/hermes-webui-src/bootstrap.py --no-browser --skip-agent-install >/tmp/hermes-webui.log 2>&1 &
  wait_for_http "http://127.0.0.1:$HERMES_WEBUI_PORT/health" 300 || true
fi

pkill -f "node /tmp/coder-mounted-proxy.mjs" >/dev/null 2>&1 || true
nohup env DASHBOARD_SESSION_HEADER=1 SYNTHETIC_HEALTHCHECK=1 TARGET_PORT=$HERMES_DASHBOARD_PORT PROXY_PORT=$HERMES_DASHBOARD_PROXY_PORT node /tmp/coder-mounted-proxy.mjs >/tmp/hermes-dashboard-proxy.log 2>&1 &
nohup env TOKEN_FILE=/home/coder/.hermes-web-ui/.token TARGET_PORT=$HERMES_WEB_UI_PORT PROXY_PORT=$HERMES_WEB_UI_PROXY_PORT node /tmp/coder-mounted-proxy.mjs >/tmp/hermes-web-ui-proxy.log 2>&1 &
nohup env TARGET_PORT=$HERMES_WEBUI_PORT PROXY_PORT=$HERMES_WEBUI_PROXY_PORT node /tmp/coder-mounted-proxy.mjs >/tmp/hermes-webui-proxy.log 2>&1 &
SH

    chmod 700 "$HERMES_BOOTSTRAP_SCRIPT"
    if [ -f "$HERMES_BOOTSTRAP_PID_FILE" ]; then
      existing_pid=$(cat "$HERMES_BOOTSTRAP_PID_FILE" 2>/dev/null || true)
      if [ -n "$existing_pid" ] && kill -0 "$existing_pid" >/dev/null 2>&1; then
        exit 0
      fi
    fi
    nohup sh "$HERMES_BOOTSTRAP_SCRIPT" >/tmp/hermes-bootstrap.log 2>&1 &
    printf '%s\n' "$!" > "$HERMES_BOOTSTRAP_PID_FILE"
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

resource "coder_app" "hermes_dashboard" {
  agent_id     = coder_agent.main.id
  slug         = "hermes-dashboard"
  display_name = "Hermes Dashboard"
  icon         = "https://raw.githubusercontent.com/NousResearch/hermes-agent/refs/heads/main/acp_registry/icon.svg"
  url          = "http://localhost:9120"
  share        = "owner"
  subdomain    = false
  order        = 2

  healthcheck {
    url       = "http://localhost:9120/health"
    interval  = 5
    threshold = 20
  }
}

resource "coder_app" "hermes_web_ui" {
  agent_id     = coder_agent.main.id
  slug         = "hermes-web-ui"
  display_name = "Hermes Web UI"
  icon         = "https://raw.githubusercontent.com/EKKOLearnAI/hermes-web-ui/refs/heads/main/packages/client/public/favicon.svg"
  url          = "http://localhost:8649"
  share        = "owner"
  subdomain    = false
  order        = 3

  healthcheck {
    url       = "http://localhost:8649/health"
    interval  = 5
    threshold = 20
  }
}

resource "coder_app" "hermes_webui" {
  agent_id     = coder_agent.main.id
  slug         = "hermes-webui"
  display_name = "Hermes WebUI Classic"
  icon         = "https://raw.githubusercontent.com/nesquena/hermes-webui/refs/heads/master/static/favicon.svg"
  url          = "http://localhost:8788"
  share        = "owner"
  subdomain    = false
  order        = 4

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

  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home_volume.name
    read_only      = false
  }
}
