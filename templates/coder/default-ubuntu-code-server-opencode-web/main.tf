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

    # Node.js 24 for the mounted-path proxy
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

    OPENCODE_WEB_PORT=4096
    OPENCODE_PROXY_PORT=4097

    nohup opencode web --hostname 127.0.0.1 --port "$OPENCODE_WEB_PORT" >/tmp/opencode-web.log 2>&1 &

    cat >/tmp/coder-mounted-proxy.mjs <<'JS'
import http from "node:http";
import net from "node:net";

const TARGET_HOST = process.env.TARGET_HOST || "127.0.0.1";
const TARGET_PORT = Number(process.env.TARGET_PORT || "0");
const PROXY_PORT = Number(process.env.PROXY_PORT || "0");

if (!TARGET_PORT || !PROXY_PORT) {
  throw new Error("TARGET_PORT and PROXY_PORT are required");
}

function rewriteHtml(html) {
  const mountScript = '<script>\n(() => {\n  let mount = location.pathname.endsWith("/") ? location.pathname.slice(0, -1) : location.pathname;\n  if (location.pathname.indexOf("/apps/") !== -1) {\n    const prefixLen = "/apps/".length;\n    const idx = location.pathname.indexOf("/apps/");\n    const afterPrefix = location.pathname.substring(idx + prefixLen);\n    const appSlug = afterPrefix.split("/")[0];\n    const trailing = appSlug.length > 0 ? appSlug.length : 0;\n    mount = location.pathname.substring(0, idx + prefixLen + trailing);\n  }\n  const pageHttpOrigin = location.origin;\n  const pageWsOrigin = pageHttpOrigin.replace(/^http/, "ws");\n  const localHosts = new Set(["127.0.0.1", "localhost"]);\n  const rewrite = (value) => {\n    const raw = value instanceof URL ? value.toString() : value;\n    if (typeof raw !== "string" || raw === "") return value;\n    if (raw.startsWith(pageHttpOrigin + mount + "/") || raw.startsWith(pageWsOrigin + mount + "/")) return raw;\n    if (raw.startsWith(pageHttpOrigin + "/")) {\n      const next = new URL(raw);\n      return pageHttpOrigin + mount + next.pathname + next.search + next.hash;\n    }\n    if (raw.startsWith(pageWsOrigin + "/")) {\n      const next = new URL(raw.replace(/^ws/, "http"));\n      return pageWsOrigin + mount + next.pathname + next.search + next.hash;\n    }\n    if (raw.startsWith("http://") || raw.startsWith("https://") || raw.startsWith("ws://") || raw.startsWith("wss://")) {\n      const next = new URL(raw.replace(/^ws/, "http"));\n      if (localHosts.has(next.hostname) || next.hostname === location.hostname) {\n        const origin = raw.startsWith("ws") ? pageWsOrigin : pageHttpOrigin;\n        return origin + mount + next.pathname + next.search + next.hash;\n      }\n      return raw;\n    }\n    if (raw.startsWith("/") && !raw.startsWith("//")) return mount + raw;\n    return raw;\n  };\n  const originalFetch = window.fetch.bind(window);\n  const requestInitFrom = async (request, init) => {\n    const method = init?.method || request.method;\n    const requestInit = {\n      method,\n      headers: init?.headers || request.headers,\n      signal: init?.signal || request.signal,\n      credentials: init?.credentials || request.credentials,\n      cache: init?.cache || request.cache,\n      mode: init?.mode || request.mode,\n      redirect: init?.redirect || request.redirect,\n      referrer: init?.referrer || request.referrer,\n      referrerPolicy: init?.referrerPolicy || request.referrerPolicy,\n      integrity: init?.integrity || request.integrity,\n      keepalive: init?.keepalive || request.keepalive,\n      ...(init || {}),\n    };\n    if (method !== "GET" && method !== "HEAD" && request.body !== null && requestInit.body === undefined && !request.bodyUsed) {\n      requestInit.body = await request.clone().arrayBuffer();\n    }\n    return requestInit;\n  };\n  window.fetch = async (input, init) => {\n    const url = rewrite(input instanceof Request ? input.url : input);\n    if (input instanceof Request) return originalFetch(url, await requestInitFrom(input, init));\n    return originalFetch(url, init);\n  };\n  const OriginalEventSource = window.EventSource;\n  window.EventSource = class extends OriginalEventSource {\n    constructor(url, config) { super(rewrite(url), config); }\n  };\n  const OriginalWebSocket = window.WebSocket;\n  window.WebSocket = class extends OriginalWebSocket {\n    constructor(url, protocols) { super(rewrite(url), protocols); }\n  };\n  const originalOpen = window.XMLHttpRequest.prototype.open;\n  window.XMLHttpRequest.prototype.open = function(method, url, ...rest) {\n    return originalOpen.call(this, method, rewrite(url), ...rest);\n  };\n  const originalPushState = window.history.pushState.bind(window.history);\n  window.history.pushState = (state, title, url) => originalPushState(state, title, url == null ? url : rewrite(url));\n  const originalReplaceState = window.history.replaceState.bind(window.history);\n  window.history.replaceState = (state, title, url) => originalReplaceState(state, title, url == null ? url : rewrite(url));\n})();\n</script>';
  const defaultProjectScript = '<script>\n(() => {\n  let mount = "";\n  const idx = location.pathname.indexOf("/apps/");\n  if (idx !== -1) {\n    const afterPrefix = location.pathname.substring(idx + "/apps/".length);\n    const appSlug = afterPrefix.split("/")[0];\n    mount = location.pathname.substring(0, idx + "/apps/".length + appSlug.length);\n  }\n  window.__OPENCODE_MOUNT = mount;\n  if (location.pathname === "/" || location.pathname === "" || (mount && (location.pathname === mount || location.pathname === mount + "/"))) {\n    window.history.replaceState(window.history.state, "", "/L2hvbWUvY29kZXI/session");\n  }\n})();\n</script>';
  const mountedBaseScript = '<script>\n(() => {\n  let mount = "";\n  const idx = location.pathname.indexOf("/apps/");\n  if (idx !== -1) {\n    const afterPrefix = location.pathname.substring(idx + "/apps/".length);\n    const appSlug = afterPrefix.split("/")[0];\n    mount = location.pathname.substring(0, idx + "/apps/".length + appSlug.length);\n  }\n  const base = document.createElement("base");\n  base.href = (mount || "") + "/";\n  document.head.prepend(base);\n  window.__OPENCODE_MOUNT = mount;\n})();\n</script>';
  return html
    .replace(/<base href="\/"\s*\/>/g, "")
    .replace(/<base href="\/"\s*>/g, "")
    .replace(/(href|src|action|content)="\//g, '$1="./')
    .replace('<link rel=\"manifest\" href=\"./site.webmanifest\" />', '')
    .replace(/(href|src)="(\.\/assets\/[^"]+\.(?:js|css))"/g, '$1="$2?coder-mount=v2"')
    .replace("<head>", "<head>" + mountedBaseScript)
    .replace("</head>", mountScript + defaultProjectScript + "</head>");
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
    .replace(/import\("\.\/([^"?]+\.js)"\)/g, 'import("./$1?coder-mount=v2")')
    .replace(/from"\.\/([^"?]+\.js)"/g, 'from"./$1?coder-mount=v2"')
    .replace('const GO="modulepreload",KO=function(e){return"/"+e},rw={},O=function', 'const GO="modulepreload",KO=function(e){let t="";const n=location.pathname.indexOf("/apps/");if(n!==-1){const r=location.pathname.substring(n+6).split("/")[0];t=location.pathname.substring(0,n+6+r.length)}return t+"/"+e+"?coder-mount=v2"},rw={},O=function')
    .replace('E(Ud,{path:"/",component:Ohe}),E(Ud,{path:"/:dir",component:ole,get children(){return[E(Ud,{path:"/",component:Rhe}),E(Ud,{path:"/session/:id?",component:Phe})]}})', 'E(Ud,{path:"/:coderUser/:coderWorkspace/apps/:coderApp",component:Ohe}),E(Ud,{path:"/:coderUser/:coderWorkspace/apps/:coderApp/:dir",component:ole,get children(){return[E(Ud,{path:"/",component:Rhe}),E(Ud,{path:"/session/:id?",component:Phe})]}}),E(Ud,{path:"/",component:Ohe}),E(Ud,{path:"/:dir",component:ole,get children(){return[E(Ud,{path:"/",component:Rhe}),E(Ud,{path:"/session/:id?",component:Phe})]}})');
}

function rewriteLocation(locationHeader) {
  if (!locationHeader) return locationHeader;
  if (locationHeader.startsWith("/")) return '.$${locationHeader}';
  if (locationHeader.startsWith("http://") || locationHeader.startsWith("https://")) {
    const next = new URL(locationHeader);
    if (next.hostname === TARGET_HOST || next.hostname === "127.0.0.1" || next.hostname === "localhost") {
      return '.$${next.pathname}$${next.search}$${next.hash}';
    }
  }
  return locationHeader;
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

const server = http.createServer((req, res) => {
  const headers = { ...req.headers };
  delete headers.host;
  delete headers["accept-encoding"];
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
        responseHeaders["Cache-Control"] = "no-store";
        delete responseHeaders.etag;
        delete responseHeaders.ETag;
        res.writeHead(upstreamRes.statusCode || 200, responseHeaders);
        res.end(payload);
      });
    },
  );
  upstream.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end('Proxy error: $${error.message}');
  });
  req.pipe(upstream);
});

server.on("upgrade", (req, socket, head) => {
  const upstream = net.connect(TARGET_PORT, TARGET_HOST, () => {
    const headerLines = [];
    headerLines.push('GET $${req.url || "/"} HTTP/$${req.httpVersion}');
    for (const [key, value] of Object.entries(req.headers)) {
      if (value == null) continue;
      if (key.toLowerCase() === "host") {
        headerLines.push('Host: $${TARGET_HOST}:$${TARGET_PORT}');
        continue;
      }
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

    nohup env TARGET_PORT="$OPENCODE_WEB_PORT" PROXY_PORT="$OPENCODE_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs >/tmp/opencode-web-proxy.log 2>&1 &
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

resource "coder_app" "opencode" {
  agent_id     = coder_agent.main.id
  slug         = "opencode"
  display_name = "OpenCode"
  icon         = "https://raw.githubusercontent.com/anomalyco/opencode/refs/heads/dev/packages/ui/src/assets/favicon/favicon-v3.svg"
  url          = "http://localhost:4097"
  share        = "owner"
  subdomain    = false
  order        = 2

  healthcheck {
    url       = "http://localhost:4097"
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
