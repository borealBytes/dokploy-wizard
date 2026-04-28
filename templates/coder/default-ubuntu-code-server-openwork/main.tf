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

    # Node.js, corepack, and pnpm are required to build the OpenWork web UI
    if ! command -v node >/dev/null 2>&1; then
      curl -fsSL https://deb.nodesource.com/setup_22.x | $_SUDO -E bash -
      $_SUDO apt-get install -y nodejs
    fi
    $_SUDO corepack enable
    $_SUDO corepack prepare pnpm@10.27.0 --activate

    # OpenWork orchestrator, skip if already installed
    if ! command -v openwork >/dev/null 2>&1; then
      $_SUDO npm install -g openwork-orchestrator
    fi

    OPENWORK_SRC_DIR=/home/coder/.cache/openwork-src
    OPENWORK_BUILD_STAMP=/home/coder/.cache/openwork-webui-build-rev
    OPENWORK_WEBUI_BUILD_KEY=v5-coder-mounted-bootstrap
    OPENWORK_UI_PORT=8790
    OPENWORK_PROXY_PORT=8788
    OPENWORK_SERVER_PORT=8787
    OPENWORK_CLIENT_TOKEN=openwork-client-token
    OPENWORK_HOST_TOKEN=openwork-host-token

    mkdir -p /home/coder/.cache

    if [ ! -d "$OPENWORK_SRC_DIR/.git" ]; then
      rm -rf "$OPENWORK_SRC_DIR"
      git clone --depth 1 --branch dev https://github.com/different-ai/openwork "$OPENWORK_SRC_DIR"
    else
      git -C "$OPENWORK_SRC_DIR" fetch --depth 1 origin dev
      git -C "$OPENWORK_SRC_DIR" checkout -f dev
      git -C "$OPENWORK_SRC_DIR" reset --hard origin/dev
    fi

    OPENWORK_REV=$(git -C "$OPENWORK_SRC_DIR" rev-parse HEAD)
    OPENWORK_BUILD_ID="$OPENWORK_REV:$OPENWORK_WEBUI_BUILD_KEY"
    OPENWORK_UI_INDEX="$OPENWORK_SRC_DIR/apps/app/dist/index.html"
    OPENWORK_NODE_MODULES="$OPENWORK_SRC_DIR/node_modules"

    if [ ! -d "$OPENWORK_NODE_MODULES" ] || [ ! -f "$OPENWORK_UI_INDEX" ] || [ ! -f "$OPENWORK_BUILD_STAMP" ] || [ "$(cat "$OPENWORK_BUILD_STAMP" 2>/dev/null || true)" != "$OPENWORK_BUILD_ID" ]; then
      cd "$OPENWORK_SRC_DIR"
      CI=true pnpm install --frozen-lockfile || CI=true pnpm install
      VITE_OPENWORK_DEPLOYMENT=web OPENWORK_PUBLIC_HOST=localhost VITE_ALLOWED_HOSTS=localhost,127.0.0.1 pnpm --filter @openwork/app exec vite build --base ./
      perl -0pi -e 's#(href|src)="/#$1="./#g' "$OPENWORK_UI_INDEX"
      printf '%s' "$OPENWORK_BUILD_ID" > "$OPENWORK_BUILD_STAMP"
    fi

    OPENWORK_APPROVAL_MODE=auto OPENWORK_PORT=$OPENWORK_SERVER_PORT OPENWORK_TOKEN="$OPENWORK_CLIENT_TOKEN" OPENWORK_HOST_TOKEN="$OPENWORK_HOST_TOKEN" nohup openwork serve --workspace /home/coder --json >/tmp/openwork.log 2>&1 &
    nohup sh -lc "cd '$OPENWORK_SRC_DIR/apps/app' && pnpm exec vite preview --host 127.0.0.1 --port $OPENWORK_UI_PORT --strictPort" >/tmp/openwork-webui.log 2>&1 &

    # Wait for openwork to start and extract owner token
    for i in $(seq 1 60); do
      if grep -q '"ownerToken"' /tmp/openwork.log 2>/dev/null; then
        break
      fi
      sleep 2
    done
    OPENWORK_OWNER_TOKEN=$(grep -o '"ownerToken": "[^"]*"' /tmp/openwork.log | head -1 | sed 's/"ownerToken": "//;s/"//')
    if [ -z "$OPENWORK_OWNER_TOKEN" ]; then
      OPENWORK_OWNER_TOKEN="$OPENWORK_CLIENT_TOKEN"
    fi

    cat >/tmp/coder-mounted-proxy.mjs <<'JS'
import http from "node:http";
import net from "node:net";

const UI_HOST = process.env.UI_HOST || "127.0.0.1";
const UI_PORT = Number(process.env.UI_PORT || "0");
const API_HOST = process.env.API_HOST || "127.0.0.1";
const API_PORT = Number(process.env.API_PORT || "0");
const PROXY_PORT = Number(process.env.PROXY_PORT || "0");
const CLIENT_TOKEN = process.env.CLIENT_TOKEN || "";

if (!UI_PORT || !API_PORT || !PROXY_PORT) {
  throw new Error("UI_PORT, API_PORT, and PROXY_PORT are required");
}

const API_PREFIXES = [
  "/health", "/status", "/capabilities", "/whoami",
  "/workspaces", "/workspace/", "/approvals", "/tokens",
  "/files/", "/opencode", "/opencode-router", "/w/",
];
const isApiPath = (pathname) => {
  const p = pathname || "/";
  if (p === "/health") return true;
  return API_PREFIXES.some((prefix) => p === prefix || p.startsWith(prefix));
};

let cachedWorkspaceId = null;
function fetchWorkspaceId() {
  return new Promise((resolve) => {
    const req = http.request({ hostname: API_HOST, port: API_PORT, path: "/workspaces", method: "GET", headers: { Authorization: `Bearer $${CLIENT_TOKEN}` } }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(Buffer.from(c)));
      res.on("end", () => {
        try {
          const data = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
          const wid = String(data.activeId || "").trim() || (data.items && data.items.length ? String(data.items[0].id || "").trim() : "");
          if (wid) cachedWorkspaceId = wid;
          resolve(wid || null);
        } catch { resolve(null); }
      });
    });
    req.on("error", () => resolve(null));
    req.end();
  });
}

function buildBootstrap(workspaceId) {
  const wid = JSON.stringify(workspaceId);
  const token = JSON.stringify(CLIENT_TOKEN);
  return '<script>\n(() => {\n'
    + 'const basePath = location.pathname.endsWith("/") ? location.pathname.slice(0, -1) : location.pathname;\n'
    + 'const baseUrl = location.origin + basePath + "/w/" + encodeURIComponent(' + wid + ');\n'
    + 'localStorage.setItem("openwork.server.urlOverride", baseUrl);\n'
    + 'localStorage.setItem("openwork.server.token", ' + token + ');\n'
    + 'localStorage.setItem("openwork.server.active", baseUrl + "/opencode");\n'
    + 'localStorage.setItem("openwork.server.list", JSON.stringify([baseUrl + "/opencode"]));\n'
    + '})();\n</script>';
}

function buildMountScript() {
  return '<script>\n(() => {\n'
    + 'let mount = location.pathname.endsWith("/") ? location.pathname.slice(0, -1) : location.pathname;\n'
    + 'if (location.pathname.indexOf("/apps/") !== -1) {\n'
    + 'const prefixLen = "/apps/".length; const idx = location.pathname.indexOf("/apps/");\n'
    + 'const afterPrefix = location.pathname.substring(idx + prefixLen);\n'
    + 'const appSlug = afterPrefix.split("/")[0];\n'
    + 'const trailing = appSlug.length > 0 ? appSlug.length : 0;\n'
    + 'mount = location.pathname.substring(0, idx + prefixLen + trailing); }\n'
    + 'const pageHttpOrigin = location.origin;\n'
    + 'const pageWsOrigin = pageHttpOrigin.replace(/^http/, "ws");\n'
    + 'const localHosts = new Set(["127.0.0.1", "localhost"]);\n'
    + 'const rewrite = (value) => { const raw = value instanceof URL ? value.toString() : value;\n'
    + 'if (typeof raw !== "string" || raw === "") return value;\n'
    + 'if (raw.startsWith(pageHttpOrigin + mount + "/") || raw.startsWith(pageWsOrigin + mount + "/")) return raw;\n'
    + 'if (raw.startsWith(pageHttpOrigin + "/")) return pageHttpOrigin + mount + new URL(raw).pathname + new URL(raw).search + new URL(raw).hash;\n'
    + 'if (raw.startsWith(pageWsOrigin + "/")) return pageWsOrigin + mount + new URL(raw.replace(/^ws/, "http")).pathname + new URL(raw).search;\n'
    + 'if (raw.startsWith("http://") || raw.startsWith("https://") || raw.startsWith("ws://") || raw.startsWith("wss://")) { const next = new URL(raw.replace(/^ws/, "http"));\n'
    + 'if (localHosts.has(next.hostname) || next.hostname === location.hostname) return (raw.startsWith("ws") ? pageWsOrigin : pageHttpOrigin) + mount + next.pathname + next.search + next.hash;\n'
    + 'return raw; }\n'
    + 'if (raw.startsWith("/") && !raw.startsWith("//")) return mount + raw; return raw; };\n'
    + 'const originalFetch = window.fetch.bind(window); window.fetch = (input, init) => { if (input instanceof Request) return originalFetch(new Request(rewrite(input.url), input), init); return originalFetch(rewrite(input), init); };\n'
    + 'window.EventSource = class extends window.EventSource { constructor(url, config) { super(rewrite(url), config); } };\n'
    + 'window.WebSocket = class extends window.WebSocket { constructor(url, protocols) { super(rewrite(url), protocols); } };\n'
    + 'const originalOpen = window.XMLHttpRequest.prototype.open; window.XMLHttpRequest.prototype.open = function(method, url, ...rest) { return originalOpen.call(this, method, rewrite(url), ...rest); };\n'
    + 'const originalPushState = window.history.pushState.bind(window.history); window.history.pushState = (state, title, url) => originalPushState(state, title, url == null ? url : rewrite(url));\n'
    + 'const originalReplaceState = window.history.replaceState.bind(window.history); window.history.replaceState = (state, title, url) => originalReplaceState(state, title, url == null ? url : rewrite(url));\n'
    + '})();\n</script>';
}

function rewriteHtml(html) {
  return html
    .replace(/(href|src|action|content)="\//g, '$1="./')
    .replace(/<base href="\/"\s*\/>/g, '<base href="./" />')
    .replace("</head>", buildMountScript() + "</head>");
}

function rewriteTextPayload(text, contentType) {
  if (contentType.includes("text/html")) return rewriteHtml(text);
  return text
    .replace(/(["'])\/assets\//g, "$1./assets/")
    .replace(/(["'])\/static\//g, "$1./static/")
    .replace(/url\(\/assets\//g, "url(./assets/")
    .replace(/url\(\/static\//g, "url(./static/");
}

function filteredHeaders(headers, isHtml) {
  const next = {};
  for (const [key, value] of Object.entries(headers)) {
    if (value == null) continue;
    const lowered = key.toLowerCase();
    if (["content-security-policy", "content-encoding", "transfer-encoding", "connection"].includes(lowered)) continue;
    if (isHtml && lowered === "content-length") continue;
    next[key] = value;
  }
  return next;
}

function proxyRequest(host, port, req, res) {
  const headers = { ...req.headers };
  delete headers.host;
  delete headers["accept-encoding"];
  const upstream = http.request({ hostname: host, port, path: req.url, method: req.method, headers }, (upstreamRes) => {
    const contentType = String(upstreamRes.headers["content-type"] || "").toLowerCase();
    const isRewrittenText = contentType.includes("text/html") || contentType.includes("javascript") || contentType.includes("ecmascript") || contentType.includes("text/css");
    if (!isRewrittenText) {
      res.writeHead(upstreamRes.statusCode || 502, filteredHeaders(upstreamRes.headers, false));
      upstreamRes.pipe(res);
      return;
    }
    const chunks = [];
    upstreamRes.on("data", (c) => chunks.push(Buffer.from(c)));
    upstreamRes.on("end", () => {
      const text = rewriteTextPayload(Buffer.concat(chunks).toString("utf-8"), contentType);
      const payload = Buffer.from(text, "utf-8");
      const respHeaders = filteredHeaders(upstreamRes.headers, true);
      respHeaders["Content-Length"] = String(payload.length);
      res.writeHead(upstreamRes.statusCode || 200, respHeaders);
      res.end(payload);
    });
  });
  upstream.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end('Proxy error: $${error.message}');
  });
  req.pipe(upstream);
}

const server = http.createServer(async (req, res) => {
  const parsed = new URL(req.url, "http://localhost");
  const pathname = parsed.pathname;
  const search = parsed.search;

  // Health check
  if (pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": "15" });
    res.end('{"status":"ok"}');
    return;
  }

  // API routes → forward to API server
  if (isApiPath(pathname)) {
    proxyRequest(API_HOST, API_PORT, req, res);
    return;
  }

  // Root path → serve bootstrapped index HTML
  if ((pathname === "" || pathname === "/") && search.indexOf("ow_url=") === -1) {
    if (!cachedWorkspaceId) await fetchWorkspaceId();
    if (!cachedWorkspaceId) {
      res.writeHead(503, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("workspace not available");
      return;
    }
    // Fetch index HTML from UI
    const uiReq = http.request({ hostname: UI_HOST, port: UI_PORT, path: "/", method: "GET" }, (uiRes) => {
      const chunks = [];
      uiRes.on("data", (c) => chunks.push(Buffer.from(c)));
      uiRes.on("end", () => {
        let html = Buffer.concat(chunks).toString("utf-8");
        const bootstrap = buildBootstrap(cachedWorkspaceId);
        if (html.includes("<head>")) {
          html = html.replace("<head>", "<head>" + bootstrap);
        } else {
          html = bootstrap + html;
        }
        html = rewriteHtml(html);
        const payload = Buffer.from(html, "utf-8");
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Content-Length": String(payload.length) });
        res.end(payload);
      });
    });
    uiReq.on("error", () => {
      res.writeHead(503);
      res.end();
    });
    uiReq.end();
    return;
  }

  // Everything else → forward to UI (Vite)
  proxyRequest(UI_HOST, UI_PORT, req, res);
});

server.on("upgrade", (req, socket, head) => {
  const parsed = new URL(req.url, "http://localhost");
  const host = isApiPath(parsed.pathname) ? API_HOST : UI_HOST;
  const port = isApiPath(parsed.pathname) ? API_PORT : UI_PORT;
  const upstream = net.connect(port, host, () => {
    const headerLines = [];
    headerLines.push('GET $${req.url || "/"} HTTP/$${req.httpVersion}');
    for (const [key, value] of Object.entries(req.headers)) {
      if (value == null) continue;
      if (key.toLowerCase() === "host") { headerLines.push('Host: $${host}:$${port}'); continue; }
      headerLines.push('$${key}: $${Array.isArray(value) ? value.join(", ") : value}');
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

    nohup env UI_PORT="$OPENWORK_UI_PORT" API_PORT="$OPENWORK_SERVER_PORT" PROXY_PORT="$OPENWORK_PROXY_PORT" CLIENT_TOKEN="$OPENWORK_OWNER_TOKEN" node /tmp/coder-mounted-proxy.mjs >/tmp/openwork-webui-proxy.log 2>&1 &
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

resource "coder_app" "openwork" {
  agent_id     = coder_agent.main.id
  slug         = "openwork"
  display_name = "OpenWork"
  icon         = "/icon/code.svg"
  url          = "http://localhost:8788"
  share        = "owner"
  subdomain    = false
  order        = 2

  healthcheck {
    url       = "http://localhost:8788/health"
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

  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home_volume.name
    read_only      = false
  }
}
