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

# Dokploy Wizard consolidated Coder workspace template.
# One workspace, every agent: VS Code (browser + desktop + terminal via the
# code-server module), OpenCode (TUI + web UI), Pi, and Hermes (classic web
# UI only), all pointed at the live LiteLLM gateway model set.

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
  username            = data.coder_workspace_owner.me.name
  hermes_desktop_slug = "hermes-desktop"
  # The webapp gets no hostname of its own: the gateway binds 127.0.0.1 with
  # webapp.public_url empty, which upstream treats as local-only (no sign-in gate),
  # and a loopback front (bootstrap below) makes the proxied Coder requests look like
  # a local browser. Declaring a public_url instead would re-engage a second sign-in:
  # upstream ties public_url to auth in the branch this listener takes, and the
  # credential it then demands is the one-use link from `hermes webapp`, which a
  # single-click Coder tile cannot chase. The perimeter is Coder share=owner plus
  # Cloudflare Access; the webapp needs to trust no public hostname at all.
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
      if ! OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/v2/install | bash; then
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

    # Pi coding agent, installed on every wizard-managed workspace
    $_SUDO corepack enable
    $_SUDO corepack prepare pnpm@10.27.0 --activate

    export PNPM_HOME=/home/coder/.local/share/pnpm
    export PATH="$PNPM_HOME/bin:$PATH"
    mkdir -p "$PNPM_HOME/bin"
    touch /home/coder/.bashrc /home/coder/.profile
    append_rc_line() {
      line="$1"
      for rc_file in /home/coder/.bashrc /home/coder/.profile; do
        grep -qxF "$line" "$rc_file" 2>/dev/null || echo "$line" >> "$rc_file"
      done
    }
    append_rc_line 'export PNPM_HOME=/home/coder/.local/share/pnpm'
    # Single-quoted on purpose. Double-quoted, $PNPM_HOME and $PATH expanded in the
    # setup shell, so the guard never matched its own output and every workspace
    # start appended another absolute copy of the build PATH to both rc files.
    append_rc_line 'export PATH="$PNPM_HOME/bin:$PATH"'
    # npm global prefix bin: where the Hermes installer lands its binary.
    append_rc_line 'export PATH="/home/coder/.local/bin:$PATH"'

    if ! command -v pi >/dev/null 2>&1; then
      pnpm add -g @earendil-works/pi-coding-agent
    fi

    command -v pi
    pi --version
    bash -lc 'command -v pi && pi --version'

    export HERMES_HOME=/home/coder/.hermes
    export HERMES_INSTALL_DIR=/home/coder/.hermes/hermes-agent
    export NPM_CONFIG_PREFIX=/home/coder/.local
    export PATH="/home/coder/.local/bin:$PATH"
    export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
    export HERMES_TEMPLATE_PROVIDER="__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__"
    export HERMES_TEMPLATE_MODEL="__DOKPLOY_WIZARD_HERMES_MODEL__"
    export HERMES_TEMPLATE_BASE_URL="__DOKPLOY_WIZARD_HERMES_BASE_URL__"
    export HERMES_TEMPLATE_API_KEY="__DOKPLOY_WIZARD_HERMES_API_KEY__"
    export HERMES_TEMPLATE_API_KEY_PLACEHOLDER="__DOKPLOY_WIZARD_HERMES_API_KEY_PLACEHOLDER__"
    export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"
    export HERMES_PUBLIC_ORIGIN="__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__"

    if ! command -v hermes >/dev/null 2>&1; then
      HERMES_HOME="$HERMES_HOME" HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup --channel "__DOKPLOY_WIZARD_HERMES_INSTALL_CHANNEL__"
    fi

    export PATH="/home/coder/.local/bin:$PATH"
    if ! command -v hermes >/dev/null 2>&1; then
      echo "Hermes installer did not produce a usable binary" >&2
      exit 1
    fi

    # Nothing read an rc file that added the npm prefix bin, so `hermes` resolved in
    # this setup script only: VS Code terminals and any process that does not inherit
    # the agent environment got "command not found". The rc lines above cover login
    # and interactive shells; the symlink covers the rest, including bash's
    # non-interactive path which returns before the appended exports.
    HERMES_BIN="$(command -v hermes)"
    $_SUDO ln -sf "$HERMES_BIN" /usr/local/bin/hermes || true
    bash -lc 'command -v hermes' >/dev/null

    mkdir -p "$HERMES_HOME" /home/coder/.cache /home/coder/.local/bin

    export HERMES_INFERENCE_PROVIDER="$${HERMES_INFERENCE_PROVIDER:-$HERMES_TEMPLATE_PROVIDER}"
    export HERMES_MODEL="$${HERMES_MODEL:-$HERMES_TEMPLATE_MODEL}"
    export OPENAI_API_BASE="$${OPENAI_API_BASE:-$HERMES_TEMPLATE_BASE_URL}"
    export OPENAI_API_KEY="$${OPENAI_API_KEY:-$HERMES_TEMPLATE_API_KEY}"
    export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-$OPENAI_API_BASE}"
    export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-$OPENAI_API_KEY}"
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

    upsert_bashrc() {
      key="$1"
      value="$2"
      file="/home/coder/.bashrc"
      marker="# dokploy-wizard-hermes-env"
      line="export $key=\"$value\""
      if [ -f "$file" ]; then
        grep -v "^export $key=" "$file" > "$file.tmp" || true
        grep -v "$marker" "$file.tmp" > "$file" || true
        rm -f "$file.tmp"
      fi
      printf '%s\n%s\n' "$marker" "$line" >> "$file"
      chown coder:coder "$file"
    }

    upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"
    upsert_env OPENAI_API_BASE "$OPENAI_API_BASE"
    upsert_env AI_DEFAULT_API_KEY "$AI_DEFAULT_API_KEY"
    upsert_env HERMES_INFERENCE_PROVIDER "$HERMES_INFERENCE_PROVIDER"
    upsert_env HERMES_MODEL "$HERMES_MODEL"
    upsert_env AI_DEFAULT_BASE_URL "$AI_DEFAULT_BASE_URL"
    upsert_env OPENCODE_GO_BASE_URL "$OPENCODE_GO_BASE_URL"
    upsert_env OPENCODE_GO_API_KEY "$OPENCODE_GO_API_KEY"
    upsert_env API_SERVER_ENABLED "$API_SERVER_ENABLED"
    upsert_env API_SERVER_HOST "$API_SERVER_HOST"
    upsert_env API_SERVER_PORT "$API_SERVER_PORT"
    upsert_env API_SERVER_KEY "$API_SERVER_KEY"

    upsert_bashrc OPENAI_API_KEY "$OPENAI_API_KEY"
    upsert_bashrc OPENAI_API_BASE "$OPENAI_API_BASE"
    upsert_bashrc AI_DEFAULT_API_KEY "$AI_DEFAULT_API_KEY"
    upsert_bashrc AI_DEFAULT_BASE_URL "$AI_DEFAULT_BASE_URL"
    upsert_bashrc HERMES_INFERENCE_PROVIDER "$HERMES_INFERENCE_PROVIDER"
    upsert_bashrc HERMES_MODEL "$HERMES_MODEL"
    upsert_bashrc OPENCODE_GO_BASE_URL "$OPENCODE_GO_BASE_URL"
    upsert_bashrc OPENCODE_GO_API_KEY "$OPENCODE_GO_API_KEY"

    if [ "$HERMES_TEMPLATE_API_KEY" = "$HERMES_TEMPLATE_API_KEY_PLACEHOLDER" ] && [ -z "$${OPENAI_API_KEY:-}" ]; then
      echo "OPENAI_API_KEY is required for the Hermes workspace template" >&2
      exit 1
    fi
    if [ -z "$OPENAI_API_KEY" ]; then
      echo "OPENAI_API_KEY is required for the Hermes workspace template" >&2
      exit 1
    fi
    if [ -z "$OPENAI_API_BASE" ]; then
      echo "OPENAI_API_BASE is required for the Hermes workspace template" >&2
      exit 1
    fi

    hermes config set model.provider "$HERMES_INFERENCE_PROVIDER"
    hermes config set model.default "$HERMES_MODEL"
    hermes config set model.base_url "$OPENAI_API_BASE"
    hermes config set terminal.backend local
    hermes config set terminal.cwd /home/coder

    mkdir -p /home/coder/.config/dokploy /home/coder/.local/share/dokploy
    cat > /home/coder/.config/dokploy/model-refresh.env <<ENVEOF
DOKPLOY_LITELLM_BASE_URL="$AI_DEFAULT_BASE_URL"
DOKPLOY_LITELLM_API_KEY="$AI_DEFAULT_API_KEY"
DOKPLOY_LITELLM_DEFAULT_ALIAS="$HERMES_MODEL"
HERMES_MODEL="$HERMES_MODEL"
HERMES_INFERENCE_PROVIDER="$HERMES_INFERENCE_PROVIDER"
DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON=\"$DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON\"
ENVEOF
    chmod 600 /home/coder/.config/dokploy/model-refresh.env
    cat > /home/coder/.local/share/dokploy/model-refresh.py <<'PY'
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

ENV_FILE = "/home/coder/.config/dokploy/model-refresh.env"
LEGACY_ENV_NAMES = {
    "DOKPLOY_LITELLM_BASE_URL": "AI_DEFAULT_BASE_URL",
    "DOKPLOY_LITELLM_API_KEY": "AI_DEFAULT_API_KEY",
    "DOKPLOY_LITELLM_DEFAULT_ALIAS": "HERMES_MODEL",
}


def setting(name: str, default: str = "") -> str:
    for candidate in (name, LEGACY_ENV_NAMES.get(name, "")):
        if candidate:
            value = os.environ.get(candidate, "").strip()
            if value:
                return value
    value = _env_file_value(name) or _env_file_value(LEGACY_ENV_NAMES.get(name, ""))
    if value:
        return value
    return default


def _restore_inner_quotes(value: str) -> str:
    if value.startswith("[") and not value.startswith('["') and value != "[]":
        # Shell/Terraform expansion strips inner double quotes from
        # substituted JSON (both in heredocs and when sourcing the env
        # file); restore them so list payloads stay parseable.
        return '["' + value[1:-1].replace(",", '","') + '"]'
    return value


def _env_file_value(name: str) -> str:
    if not name:
        return ""
    try:
        with open(ENV_FILE, encoding="utf-8") as handle:
            for line in handle:
                key, separator, raw_value = line.partition("=")
                if separator and key.strip() == name:
                    return _restore_inner_quotes(raw_value.strip().strip('"'))
    except OSError:
        pass
    return ""


base_url = setting("DOKPLOY_LITELLM_BASE_URL").rstrip("/")
api_key = setting("DOKPLOY_LITELLM_API_KEY")
home = Path(os.environ.get("HERMES_HOME", "/home/coder/.hermes"))
config_path = home / "config.yaml"
provider = setting("HERMES_INFERENCE_PROVIDER", "dokploy-litellm") or "dokploy-litellm"
default_model = setting("HERMES_MODEL")
def _parse_fallback_models(raw, _depth=0):
    if not raw or _depth > 3:
        return []
    try:
        value = json.loads(_restore_inner_quotes(raw))
    except ValueError:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str):
        # Terraform/shell quote-stripping can leave a JSON *string* wrapping the
        # list text; unwrap and reparse instead of iterating it character-wise.
        return _parse_fallback_models(value, _depth + 1)
    return []

fallback_models = _parse_fallback_models(
    setting("DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON", "")
)

if "--daemon" in sys.argv[1:]:
    # The boot-time refresh already ran before the daemon starts, so the first
    # steady-state wait is the full interval. A failed refresh (gateway down)
    # retries after 5 minutes and returns to the slow cadence once healed.
    time.sleep(43200)
    while True:
        refresh_rc = subprocess.run([sys.executable, __file__], check=False).returncode
        time.sleep(300 if refresh_rc else 43200)


def fetch_model_ids() -> list[str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{base_url}/v1/models", headers=headers)
    model_ids = []
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            payload = {"data": []}

        model_ids = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str):
                continue
            normalized = model_id.strip()
            if normalized and "/" in normalized and not normalized.endswith("/*") and not normalized.startswith("openai/"):
                model_ids.append(normalized)
        if model_ids or attempt == 3:
            return model_ids
        time.sleep(10)
    return model_ids


fetched_model_ids = fetch_model_ids()
if not fetched_model_ids and Path("/home/coder/.config/opencode/opencode.json").exists():
    # Gateway unreachable during a background refresh; keep the last good list
    # and exit non-zero so the daemon schedules a fast retry.
    raise SystemExit(3)

model_ids = fetched_model_ids
for model_id in fallback_models:
    if isinstance(model_id, str) and model_id.strip():
        model_ids.append(model_id.strip())
if default_model not in model_ids:
    model_ids.insert(0, default_model)
model_ids = list(dict.fromkeys(model_ids))

config = {}
if config_path.exists():
    try:
        if yaml is not None:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        else:
            config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
if not isinstance(config, dict):
    config = {}

config["model"] = {
    "provider": provider,
    "default": default_model,
    "base_url": base_url,
}
providers = config.setdefault("providers", {})
if not isinstance(providers, dict):
    providers = {}
    config["providers"] = providers
providers[provider] = {
    "name": "Dokploy LiteLLM",
    "base_url": base_url,
    "api_key": api_key,
    "model": default_model,
    "default_model": default_model,
    "transport": "chat_completions",
    "models": {model_id: {} for model_id in model_ids},
    "discover_models": False,
}

home.mkdir(parents=True, exist_ok=True)
if yaml is not None:
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
else:
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
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

# Cost hints for the pickers: models.dev prices every model in dollars per
# million input/output tokens, and gateway aliases map back onto that
# registry through their sync/ (or provider) prefix. The registry is cached
# daily so the daemon's twice-daily pass does not refetch the full catalog.
MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_CACHE = Path("/home/coder/.cache/dokploy-models-dev.json")
ALIAS_PROVIDERS = {
    "sync/nvidia/": "nvidia",
    "sync/openrouter/": "openrouter",
    "opencode-go/": "opencode-go",
    "opencode/": "opencode",
    "nvidia/": "nvidia",
}


def _registry_costs():
    cache = None
    try:
        if time.time() - MODELS_DEV_CACHE.stat().st_mtime < 86400:
            cache = json.loads(MODELS_DEV_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = None
    if cache is None:
        try:
            # models.dev (Cloudflare) rejects the default Python-urllib agent.
            request = urllib.request.Request(
                MODELS_DEV_URL,
                headers={"Accept": "application/json", "User-Agent": "dokploy-wizard/model-refresh"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                cache = json.load(response)
            MODELS_DEV_CACHE.parent.mkdir(parents=True, exist_ok=True)
            MODELS_DEV_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        except Exception:
            try:
                cache = json.loads(MODELS_DEV_CACHE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
    costs = {}
    if not isinstance(cache, dict):
        return costs
    for provider_key, block in cache.items():
        block_models = block.get("models") if isinstance(block, dict) else None
        if not isinstance(block_models, dict):
            continue
        for model_id, meta in block_models.items():
            cost = meta.get("cost") if isinstance(meta, dict) else None
            if not isinstance(cost, dict):
                continue
            try:
                entry = {
                    "input": float(cost.get("input", 0) or 0),
                    "output": float(cost.get("output", 0) or 0),
                }
            except (TypeError, ValueError):
                continue
            costs[f"{provider_key}/{model_id}"] = entry
    return costs


def _cost_for(alias, costs):
    if alias.endswith(":free"):
        return {"input": 0, "output": 0}
    for prefix, provider_key in ALIAS_PROVIDERS.items():
        if alias.startswith(prefix):
            return costs.get(provider_key + "/" + alias[len(prefix):])
    return None


registry_costs = _registry_costs()


def _fmt_price(value):
    text = ("%.6f" % value).rstrip("0").rstrip(".")
    return text if text else "0"


def _model_label(model_id, cost):
    if cost["input"] == 0 and cost["output"] == 0:
        return model_id + " (free)"
    # The dollar-dollar prefix is Terraform's escape for a literal dollar-
    # brace; this script lives inside a TF heredoc that interpolates them.
    return model_id + f" (in:$${_fmt_price(cost['input'])}/out:$${_fmt_price(cost['output'])})"


def _priced_models():
    entries = {}
    for model_id in model_ids:
        cost = _cost_for(model_id, registry_costs)
        if not cost:
            entries[model_id] = {}
            continue
        entries[model_id] = {"name": _model_label(model_id, cost), "cost": cost}
    return entries


def _pi_model_entry(model_id):
    entry = {"id": model_id, "name": model_id}
    cost = _cost_for(model_id, registry_costs)
    if cost:
        entry["name"] = _model_label(model_id, cost)
        entry["cost"] = {
            "input": cost["input"],
            "output": cost["output"],
            "cacheRead": 0,
            "cacheWrite": 0,
        }
    return entry


Path("/home/coder/.config/opencode").mkdir(parents=True, exist_ok=True)
opencode_config = {
    "provider": {
        "litellm": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": base_url, "apiKey": api_key},
            "models": _priced_models(),
        }
    },
    "model": default_model,
}
Path("/home/coder/.config/opencode/opencode.json").write_text(
    json.dumps(opencode_config, indent=2) + "\n",
    encoding="utf-8",
)

Path("/home/coder/.pi/agent").mkdir(parents=True, exist_ok=True)
pi_models_config = {
    "providers": {
        "litellm": {
            "name": "LiteLLM",
            "baseUrl": base_url,
            "api": "openai-completions",
            "apiKey": api_key,
            "models": [_pi_model_entry(model_id) for model_id in model_ids],
        }
    }
}
Path("/home/coder/.pi/agent/models.json").write_text(
    json.dumps(pi_models_config, indent=2) + "\n",
    encoding="utf-8",
)

# Signal gateway-fetch failure (exit 3) so the daemon backoffs to its fast
# retry; the config writes above still reflect best-effort state.
raise SystemExit(0 if fetched_model_ids else 3)

PY

    sh -c 'set -a; . /home/coder/.config/dokploy/model-refresh.env; set +a; exec python3 /home/coder/.local/share/dokploy/model-refresh.py' || true
    if ! pgrep -f 'model-refresh.py --daemon' >/dev/null 2>&1; then
      setsid nohup python3 /home/coder/.local/share/dokploy/model-refresh.py --daemon >>/tmp/model-refresh-daemon.log 2>&1 < /dev/null &
    fi

    OPENCODE_WEB_PORT=4096

    # opencode serve mints a random server password every boot, which kills
    # browser pairings on every restart. The wizard seeds a stable password
    # (the stack admin password) into the persistent home volume; browsers
    # connect with server URL + this password, and the pairing survives
    # rebuilds. Override per workspace by editing the file and restarting
    # the serve process.
    OPENCODE_SERVER_PW_FILE=/home/coder/.local/share/dokploy/opencode-server-password
    # A released build shipped this file holding an unreplaced replacement token, because
    # an unreplaced token breaks no Terraform plan: it silently became the serve password.
    # Rewrite exactly those files; a password someone chose by hand is left alone.
    if [ ! -s "$OPENCODE_SERVER_PW_FILE" ] || grep -q '__DOKPLOY_WIZARD_' "$OPENCODE_SERVER_PW_FILE"; then
      mkdir -p "$(dirname "$OPENCODE_SERVER_PW_FILE")"
      printf '%s' "__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__" > "$OPENCODE_SERVER_PW_FILE"
    fi
    export OPENCODE_SERVER_PASSWORD="$(cat "$OPENCODE_SERVER_PW_FILE")"

    nohup opencode serve --hostname 127.0.0.1 --port "$OPENCODE_WEB_PORT" >/tmp/opencode-web.log 2>&1 &

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
    if (res.headersSent) {
      // Mid-stream failure after bytes started flowing: the status line is gone.
      res.destroy();
      return;
    }
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end(`Proxy error: $${error.message}`);
  });
  req.pipe(upstream);
  // Coder's agent drops the downstream socket whenever a tab closes or an SSE
  // stream is interrupted, and nothing on that path touches the upstream request:
  // Node's pipe() does not destroy its source either. Without an explicit teardown
  // the upstream keeps streaming into a dead socket, and the target's
  // per-connection worker model turns those zombie streams into worker exhaustion
  // and a blanket 503 for every later request.
  const teardownUpstream = () => {
    if (!upstream.destroyed) upstream.destroy();
  };
  res.on("close", teardownUpstream);
  req.on("aborted", teardownUpstream);
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
  // WebSocket pairs must die together, or a dead browser socket leaves the
  // upstream socket half-open for the same reason as the request path above.
  socket.on("error", () => upstream.destroy());
  socket.on("close", () => upstream.destroy());
  upstream.on("close", () => socket.destroy());
});

// Long-lived SSE and WebSocket connections are the reason this proxy exists, so
// the 300s default request timeout must not fire mid-stream; idle keep-alive
// sockets are still reaped.
server.requestTimeout = 0;
server.keepAliveTimeout = 30000;
server.headersTimeout = 60000;
server.listen(PROXY_PORT, "127.0.0.1");
JS

    export HERMES_WEBUI_PORT=8787
    export HERMES_WEBUI_PROXY_PORT=8788
    HERMES_BOOTSTRAP_SCRIPT=/tmp/hermes-workspace-bootstrap.sh
    HERMES_BOOTSTRAP_PID_FILE=/tmp/hermes-workspace-bootstrap.pid

    cat >"$HERMES_BOOTSTRAP_SCRIPT" <<'SH'
set -e

export HERMES_HOME="$${HERMES_HOME:-/home/coder/.hermes}"
export HERMES_INSTALL_DIR="$${HERMES_INSTALL_DIR:-/home/coder/.hermes/hermes-agent}"
export NPM_CONFIG_PREFIX="$${NPM_CONFIG_PREFIX:-/home/coder/.local}"
export PATH="/home/coder/.local/bin:$PATH"
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export HERMES_TEMPLATE_PROVIDER="$${HERMES_TEMPLATE_PROVIDER:-__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__}"
export HERMES_TEMPLATE_MODEL="$${HERMES_TEMPLATE_MODEL:-__DOKPLOY_WIZARD_HERMES_MODEL__}"
export HERMES_TEMPLATE_BASE_URL="$${HERMES_TEMPLATE_BASE_URL:-__DOKPLOY_WIZARD_HERMES_BASE_URL__}"
export HERMES_TEMPLATE_API_KEY="$${HERMES_TEMPLATE_API_KEY:-__DOKPLOY_WIZARD_HERMES_API_KEY__}"
export HERMES_TEMPLATE_API_KEY_PLACEHOLDER="$${HERMES_TEMPLATE_API_KEY_PLACEHOLDER:-__DOKPLOY_WIZARD_HERMES_API_KEY_PLACEHOLDER__}"
export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="$${DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON:-__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__}"
export HERMES_PUBLIC_ORIGIN="$${HERMES_PUBLIC_ORIGIN:-__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__}"
export HERMES_WEBAPP_INTERNAL_PORT="$${HERMES_WEBAPP_INTERNAL_PORT:-9121}"
export HERMES_INFERENCE_PROVIDER="$${HERMES_INFERENCE_PROVIDER:-$HERMES_TEMPLATE_PROVIDER}"
export HERMES_MODEL="$${HERMES_MODEL:-$HERMES_TEMPLATE_MODEL}"
export OPENAI_API_BASE="$${OPENAI_API_BASE:-$HERMES_TEMPLATE_BASE_URL}"
export OPENAI_API_KEY="$${OPENAI_API_KEY:-$HERMES_TEMPLATE_API_KEY}"
export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-$OPENAI_API_BASE}"
export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-$OPENAI_API_KEY}"
export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$AI_DEFAULT_BASE_URL}"
export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$AI_DEFAULT_API_KEY}"
export API_SERVER_ENABLED="$${API_SERVER_ENABLED:-true}"
export API_SERVER_HOST="$${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="$${API_SERVER_PORT:-8642}"
export API_SERVER_KEY="$${API_SERVER_KEY:-hermes-local-api-key}"
export HERMES_WEBUI_PORT="$${HERMES_WEBUI_PORT:-8787}"
export HERMES_WEBUI_PROXY_PORT="$${HERMES_WEBUI_PROXY_PORT:-8788}"
export HERMES_WEBAPP_PORT="$${HERMES_WEBAPP_PORT:-9120}"
export HERMES_WEBAPP_HEALTH_PATH="/api/health"
# The sign-in name is the owner's Coder username, not a fixed word: the login page
# appears before any workspace file is readable, so "Invalid username or password"
# with a name nobody could guess is a dead end. The password is the stack admin
# password the operator already uses for LiteLLM and Dokploy.
export HERMES_WEBAPP_BASIC_AUTH_USERNAME="${local.username}"

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
  HERMES_HOME="$HERMES_HOME" HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup --channel "__DOKPLOY_WIZARD_HERMES_INSTALL_CHANNEL__"
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

upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"
upsert_env OPENAI_API_BASE "$OPENAI_API_BASE"
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
upsert_env_if_unset() {
  key="$1"
  value="$2"
  file="$HERMES_HOME/.env"
  if [ -f "$file" ]; then
    existing=$(grep "^$${key}=" "$file" | tail -1 | cut -d= -f2-)
    if [ -n "$existing" ]; then
      return 0
    fi
  fi
  upsert_env "$key" "$value"
}

upsert_env HERMES_WEBUI_SKIP_ONBOARDING "1"
upsert_env HERMES_WEBUI_DEFAULT_MODEL "$HERMES_MODEL"

cat > /home/coder/.config/dokploy/model-refresh.env <<ENVEOF
DOKPLOY_LITELLM_BASE_URL="$AI_DEFAULT_BASE_URL"
DOKPLOY_LITELLM_API_KEY="$AI_DEFAULT_API_KEY"
DOKPLOY_LITELLM_DEFAULT_ALIAS="$HERMES_MODEL"
HERMES_MODEL="$HERMES_MODEL"
HERMES_INFERENCE_PROVIDER="$HERMES_INFERENCE_PROVIDER"
DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON=\"$DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON\"
ENVEOF
chmod 600 /home/coder/.config/dokploy/model-refresh.env
if [ -f /home/coder/.local/share/dokploy/model-refresh.py ]; then
  # Refresh tool configs from the live gateway using the shared refresher script.
  sh -c 'set -a; . /home/coder/.config/dokploy/model-refresh.env; set +a; exec python3 /home/coder/.local/share/dokploy/model-refresh.py' || true
fi

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

NESQ_DIR=/home/coder/.cache/hermes-webui-src
if [ ! -d "$NESQ_DIR/.git" ]; then
  rm -rf "$NESQ_DIR"
  git clone --depth 1 --branch master https://github.com/nesquena/hermes-webui "$NESQ_DIR"
else
  git -C "$NESQ_DIR" fetch --depth 1 origin master
  git -C "$NESQ_DIR" checkout -f master
  git -C "$NESQ_DIR" reset --hard origin/master
fi

# Hermes Desktop webapp: the Desktop app's own renderer, served by the gateway on this
# port so it runs in a browser. It is NOT `hermes dashboard`: the dashboard is the admin
# panel, a different bundle (hermes_cli/web_dist) whose chat tab is a terminal over a
# PTY. Nothing in this template puts the dashboard on the tile port.
#
# The browser-hosted Desktop merged upstream on 2026-10-25, after the newest published
# release, so the workspace installs the channel that carries it. A build without the
# command is repaired before anything is served, because the failure this block exists
# to prevent is the tile quietly serving the old dashboard again.
HERMES_DESKTOP_CHANNEL="$${HERMES_DESKTOP_CHANNEL:-__DOKPLOY_WIZARD_HERMES_INSTALL_CHANNEL__}"
if ! hermes webapp --help >/dev/null 2>&1; then
  echo "installed Hermes build has no 'hermes webapp'; moving onto channel $HERMES_DESKTOP_CHANNEL"
  # --set-channel moves the checkout onto the channel AND records it for this install,
  # so a later `hermes update` cannot pull the workspace back onto a release that
  # predates the Webapp. If that persisted without moving, ask for the move explicitly.
  hermes update --set-channel "$HERMES_DESKTOP_CHANNEL" --yes --no-gateway-restart >/tmp/hermes-webapp-update.log 2>&1 || true
  if ! hermes webapp --help >/dev/null 2>&1; then
    hermes update --channel "$HERMES_DESKTOP_CHANNEL" --yes --no-gateway-restart >>/tmp/hermes-webapp-update.log 2>&1 || true
  fi
fi
# An older hermes stores webapp.* keys with a warning it never reads, so config and
# build are skipped rather than writing keys into a build that ignores them: an
# unhealthy tile with a reason beats a healthy tile lying about what it serves.
HERMES_WEBAPP_CAPABLE=1
if ! hermes webapp --help >/dev/null 2>&1; then
  HERMES_WEBAPP_CAPABLE=0
  echo "HERMES WEBAPP UNAVAILABLE: 'hermes webapp' is still missing after the channel update; serving nothing on $HERMES_WEBAPP_PORT rather than the dashboard" >&2
fi

# A gated dashboard fails closed: on a non-loopback bind it always requires a sign-in
# provider, so the basic-auth trio is written every boot rather than conditionally.
# The trio is read from $HERMES_HOME/.env, which hermes loads into its environment.
# Rewritten every boot (not if-unset): the name is derived from the workspace owner,
# so a volume built when this was a fixed word has to follow the owner's name. The
# password and signing secret below are the opposite case and stay as they are.
upsert_env HERMES_DASHBOARD_BASIC_AUTH_USERNAME "$HERMES_WEBAPP_BASIC_AUTH_USERNAME"
upsert_env_if_unset HERMES_DASHBOARD_BASIC_AUTH_PASSWORD "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__"
# An empty secret mints a new signing key on every boot and logs every session out on
# each restart, so the first boot's secret is kept for the life of the volume.
upsert_env_if_unset HERMES_DASHBOARD_BASIC_AUTH_SECRET "$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
chmod 600 "$HERMES_HOME/.env"

# The gateway reads webapp.* and its sign-in providers only when it starts, so these are
# written before anything starts, on every boot.
#
# Loopback bind on the INTERNAL port, public_url always empty: that is the one shape
# upstream serves without a sign-in gate (gateway/run_api.py engages auth whenever
# webapp.public_url names a host, with no exception for this listener). The webapp
# still validates Host, and while loopback-bound it accepts only loopback names —
# which is exactly what the front below sends it. Cleared on every boot rather than
# left alone: a public_url surviving from an earlier design would silently re-arm
# both the gate and the Host rejection, and the tile would spend the boot answering
# 302 and 400 to a browser that only wants to open.
if [ "$HERMES_WEBAPP_CAPABLE" = "1" ]; then
  hermes config set webapp.enabled true
  hermes config set webapp.host 127.0.0.1
  hermes config set webapp.port "$HERMES_WEBAPP_INTERNAL_PORT"
  hermes config set webapp.public_url ""
fi

# The renderer build is npm and several minutes, and the checkout only changes when the
# Hermes version moves, so rebuild only when that version changes. Tracked in the
# persistent home volume, not /tmp. The artifact is apps/desktop/dist-webapp (stamped
# with hermes-build.json); it is the directory the gateway serves.
HERMES_WEBAPP_DIST="$HERMES_HOME/hermes-agent/apps/desktop/dist-webapp"
HERMES_WEBAPP_BUILD_MARKER=/home/coder/.local/share/dokploy/hermes-webapp-bundle-version
mkdir -p "$(dirname "$HERMES_WEBAPP_BUILD_MARKER")"
HERMES_WEBAPP_VERSION="$(hermes --version 2>/dev/null | head -1 | tr -cd '[:alnum:]._-')"
if [ "$HERMES_WEBAPP_CAPABLE" = "1" ] && { [ ! -s "$HERMES_WEBAPP_BUILD_MARKER" ] || [ "$(cat "$HERMES_WEBAPP_BUILD_MARKER" 2>/dev/null)" != "$HERMES_WEBAPP_VERSION" ]; }; then
  echo "building Hermes Desktop webapp bundle (version $${HERMES_WEBAPP_VERSION:-unknown})"
  # Upstream's own build entry point: receipt-based, so it rebuilds when the renderer
  # sources changed and costs nothing when they did not.
  if hermes webapp --build-only >/tmp/hermes-webapp-build.log 2>&1; then
    printf '%s' "$HERMES_WEBAPP_VERSION" >"$HERMES_WEBAPP_BUILD_MARKER"
  else
    # Upstream drift can move the build; the community WebUI tile is the safety net, so
    # a failed bundle build costs the official tile instead of the workspace.
    echo "webapp bundle build failed; see /tmp/hermes-webapp-build.log" >&2
    tail -20 /tmp/hermes-webapp-build.log >&2 || true
  fi
fi

if ! wait_for_http "http://127.0.0.1:$API_SERVER_PORT/health" 1; then
  nohup hermes gateway >/tmp/hermes-gateway.log 2>&1 &
  wait_for_http "http://127.0.0.1:$API_SERVER_PORT/health" 300 || true
fi

# The webapp rides the gateway's own listener: there is no separate server process to
# start, and the gateway is already brought up above for its API port.
if [ "$HERMES_WEBAPP_CAPABLE" = "1" ]; then
  if ! hermes webapp --status >/tmp/hermes-webapp-status.log 2>&1; then
    # rc=1 means this listener is not serving the Webapp, and upstream names its reasons
    # (off, or a gateway that started before it was turned on). Its own restart is the
    # remedy: it reloads webapp.* and brings the listener onto this version. Left in the
    # background, because on a cold boot the gateway is still coming up and the wait
    # below is what has to prove the tile is really served.
    nohup hermes gateway restart >/tmp/hermes-gateway-restart.log 2>&1 &
  fi
fi

# The tile's front: a loopback forwarder that owns the tile port and hands requests to
# the webapp looking like a local browser — Host rewritten to the bound address, Origin
# dropped, forwarded-for headers dropped. The webapp validates Host on HTTP and
# revalidates Host and Origin on the WebSocket upgrade (web_server_boundary.py measures
# both against the bind while auth is off), so a proxied request carrying the tile
# hostname would be refused on every path; after the front it is refused on none.
#
# It also signs the browser in — the part a curl-level proof misses. The Desktop bundle
# keeps its credential in sessionStorage, normally placed there by the one-use launch
# link from `hermes webapp` (browser-launch-session.ts raises WEBAPP_LAUNCH_REQUIRED
# without it and renders a dead "Sign in to Hermes" card). A tile browser is already
# authenticated upstream by Coder and cannot run that command, so the front reads the
# gateway's stored browser credential from $HERMES_HOME/webapp/session-token and
# injects the same sessionStorage grant into the entry page (text/html only, skipped
# when the page already carries a grant, e.g. from the patched listener itself). The
# perimeter argument is unchanged: the only reachable path to the listener is Coder
# behind Cloudflare Access, which is exactly the browser this hands the credential to.
if [ "$HERMES_WEBAPP_CAPABLE" = "1" ]; then
  # Serve-side opt-in for the webapp's own trust-proxy patch (marker-only here; the
  # patch itself lives in the Hermes checkout and may or may not be applied). With the
  # patch present the listener injects the grant itself and the front stands down;
  # without it the front injects. Either way the tile works after a rebuild.
  mkdir -p "$HERMES_HOME/webapp"
  printf '%s\n' "The Webapp on this loopback listener is reached through an authenticating proxy." \
    "Its browsers are signed in by that proxy, so the page carries the browser credential." \
    >"$HERMES_HOME/webapp/trust-proxy"
  chmod 600 "$HERMES_HOME/webapp/trust-proxy"

  HERMES_WEBAPP_FRONT_FILE=/home/coder/.local/share/dokploy/hermes-webapp-front.mjs
  mkdir -p "$(dirname "$HERMES_WEBAPP_FRONT_FILE")"
  cat >"$HERMES_WEBAPP_FRONT_FILE" <<'MJS'
#!/usr/bin/env node
// hermes-webapp-front: presents the Desktop webapp on the tile port. Loopback in,
// loopback out; rewrites what the upstream boundary check looks at, and hands the
// browser the credential a launch link would have left there.
import http from "node:http";
import net from "node:net";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

const BACKEND_PORT = Number.parseInt(process.env.HERMES_WEBAPP_INTERNAL_PORT || "9121", 10);
const FRONT_PORT = Number.parseInt(process.env.PROXY_PORT || "9120", 10);
const BACKEND_HOST = `127.0.0.1:$${BACKEND_PORT}`;
const SESSION_KEY = "hermes.webapp.session.v1";
const TOKEN_FILE = process.env.HERMES_WEBAPP_TOKEN_FILE
  || path.join(process.env.HERMES_HOME || path.join(os.homedir(), ".hermes"), "webapp", "session-token");

function proxiedHeaders(req) {
  const headers = { ...req.headers, host: BACKEND_HOST };
  delete headers.origin;
  delete headers["x-forwarded-for"];
  delete headers["x-forwarded-host"];
  delete headers["x-forwarded-proto"];
  return headers;
}

function signInScript() {
  let token = "";
  try {
    token = fs.readFileSync(TOKEN_FILE, "utf8").trim();
  } catch {
    return "";
  }
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) return "";
  return `/* authenticated upstream: sign this browser in */try{sessionStorage.setItem("$${SESSION_KEY}:"+JSON.stringify([location.origin,window.__HERMES_BASE_PATH__||""]),$${JSON.stringify(token)})}catch(e){}`;
}

// The webapp serves its entry page with a CSP that allowlists inline scripts by
// sha256. A patched listener injects inside a script block it hashes itself; a
// front proxy injecting its own <script> must declare that script's hash in the
// response's script-src directive, or the browser silently refuses it (which
// reproduces the sign-in card the injection exists to remove).
function allowInCsp(csp, scriptInner) {
  if (!csp) return csp;
  const hash = `'sha256-$${crypto.createHash("sha256").update(scriptInner, "utf8").digest("base64")}'`;
  if (csp.includes(hash)) return csp;
  let touched = false;
  const out = csp.split(";").map((part) => {
    if (/^\s*script-src\s/.test(part)) { touched = true; return `${part.trimEnd()} $${hash}`; }
    return part;
  });
  return touched ? out.join(";") : csp;
}

function injectSignIn(html, headers) {
  if (html.includes(SESSION_KEY)) return html;
  const inner = signInScript();
  if (!inner) return html;
  headers["content-security-policy"] = allowInCsp(headers["content-security-policy"], inner);
  const script = `<script>$${inner}</script>`;
  const head = html.match(/<head[^>]*>/i);
  return head ? html.slice(0, head.index + head[0].length) + script + html.slice(head.index + head[0].length)
    : script + html;
}

const server = http.createServer((req, res) => {
  const up = http.request(
    { host: "127.0.0.1", port: BACKEND_PORT, path: req.url, method: req.method, headers: proxiedHeaders(req) },
    (ur) => {
      const type = String(ur.headers["content-type"] || "");
      const buffer = req.method !== "HEAD" && type.startsWith("text/html") && !ur.headers["content-encoding"];
      if (!buffer) {
        res.writeHead(ur.statusCode || 502, ur.headers);
        ur.pipe(res);
        return;
      }
      const chunks = [];
      ur.on("data", (chunk) => chunks.push(chunk));
      ur.on("end", () => {
        const headers = { ...ur.headers };
        const body = injectSignIn(Buffer.concat(chunks).toString("utf8"), headers);
        delete headers["content-length"];
        delete headers.etag;
        headers["content-length"] = Buffer.byteLength(body);
        res.writeHead(ur.statusCode || 502, headers);
        res.end(body);
      });
    },
  );
  up.on("error", () => {
    if (!res.headersSent) res.writeHead(502, { "content-type": "application/json" });
    res.end('{"detail":"hermes webapp backend unavailable"}');
  });
  req.pipe(up);
});
server.on("upgrade", (req, socket, head) => {
  const headers = proxiedHeaders(req);
  const raw = Object.entries(headers)
    .map(([k, v]) => `$${k}: $${Array.isArray(v) ? v.join(", ") : v}`)
    .join("\r\n");
  const upstream = net.connect(BACKEND_PORT, "127.0.0.1", () => {
    upstream.write(`$${req.method} $${req.url} HTTP/1.1\r\n$${raw}\r\n\r\n`);
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on("error", () => socket.destroy());
  socket.on("error", () => upstream.destroy());
});
server.listen(FRONT_PORT, "127.0.0.1", () => console.log(`[hermes-webapp-front] 127.0.0.1:$${FRONT_PORT} -> $${BACKEND_HOST}`));
MJS
  # Bracketed pattern: a shell whose argv carries this script must not match its own kill.
  pkill -f "hermes-webapp-front[.]mjs" >/dev/null 2>&1 || true
  nohup env HERMES_HOME="$HERMES_HOME" HERMES_WEBAPP_INTERNAL_PORT="$HERMES_WEBAPP_INTERNAL_PORT" PROXY_PORT="$HERMES_WEBAPP_PORT" node "$HERMES_WEBAPP_FRONT_FILE" >/tmp/hermes-webapp-front.log 2>&1 &
fi

# A dead front and a bind that refuses the port both look like a dead tile from
# outside, so report what the process itself claims. The status line carries the whole
# interpreter invocation, which is hundreds of characters of noise. /api/health is the
# endpoint the webapp answers without a session, reached here through the front, which
# is why both this wait and the tile healthcheck use it: they then prove the whole
# chain the browser will traverse, not just one process of it.
wait_for_http "http://127.0.0.1:$HERMES_WEBAPP_PORT$HERMES_WEBAPP_HEALTH_PATH" 300 || true
echo "Hermes Desktop webapp: no sign-in of its own; open it from the Coder tile (perimeter: Coder login + Cloudflare Access)"
hermes webapp --status 2>&1 | sed -n '1,2p' | cut -c1-160 | sed 's/^/[hermes webapp] /' || true
# end-webapp-bootstrap-block

if ! wait_for_http "http://127.0.0.1:$HERMES_WEBUI_PORT/health" 1; then
  nohup env HERMES_HOME="$HERMES_HOME" HERMES_WEBUI_ALLOWED_ORIGINS="$HERMES_PUBLIC_ORIGIN" HERMES_WEBUI_TRUST_FORWARDED_HOST=1 HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=$HERMES_WEBUI_PORT HERMES_WEBUI_AGENT_DIR=$HERMES_INSTALL_DIR HERMES_WEBUI_STATE_DIR="$HERMES_HOME/webui" HERMES_WEBUI_SKIP_ONBOARDING=1 HERMES_WEBUI_DEFAULT_MODEL="$HERMES_MODEL" PYTHONPATH="$HERMES_INSTALL_DIR:$${PYTHONPATH:-}" python3 /home/coder/.cache/hermes-webui-src/bootstrap.py --no-browser --skip-agent-install >/tmp/hermes-webui.log 2>&1 &
fi

pkill -f "node /tmp/coder-mounted-proxy.mjs" >/dev/null 2>&1 || true
nohup env TARGET_PORT=$HERMES_WEBUI_PORT PROXY_PORT=$HERMES_WEBUI_PROXY_PORT node /tmp/coder-mounted-proxy.mjs >/tmp/hermes-webui-proxy.log 2>&1 &

if ! pgrep -f 'model-refresh.py --daemon' >/dev/null 2>&1; then
  setsid nohup python3 /home/coder/.local/share/dokploy/model-refresh.py --daemon >>/tmp/model-refresh-daemon.log 2>&1 < /dev/null &
fi

wait_for_http "http://127.0.0.1:$HERMES_WEBUI_PORT/health" 60 || true

# The WebUI and its proxy die quietly: an upstream crash or a wedged stream takes
# the process down and nothing brings it back, so the tile keeps answering 502
# until someone rebuilds the workspace. Everything below is revived by one script.
HERMES_WATCHDOG_SCRIPT=/home/coder/.local/share/dokploy/hermes-webui-watchdog.sh
mkdir -p /home/coder/.local/share/dokploy
cat >"$HERMES_WATCHDOG_SCRIPT" <<WDSH
#!/usr/bin/env bash
# Revives the Hermes gateway, WebUI, and Coder mounted proxy when they are down.
# Cron runs it once a minute where a cron daemon is available; the bootstrap also
# keeps a --supervise loop running, because the workspace container has no init to
# start cron for us. Values baked in here come from the bootstrap environment.
set -u

HERMES_HOME="$HERMES_HOME"
HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR"
HERMES_MODEL="$HERMES_MODEL"
HERMES_PUBLIC_ORIGIN="$HERMES_PUBLIC_ORIGIN"
HERMES_WEBUI_PORT="$HERMES_WEBUI_PORT"
HERMES_WEBUI_PROXY_PORT="$HERMES_WEBUI_PROXY_PORT"
HERMES_WEBAPP_PORT="$HERMES_WEBAPP_PORT"
HERMES_WEBAPP_INTERNAL_PORT="$HERMES_WEBAPP_INTERNAL_PORT"
HERMES_WEBAPP_HEALTH_PATH="$HERMES_WEBAPP_HEALTH_PATH"
API_SERVER_PORT="$API_SERVER_PORT"
WEBUI_SRC=/home/coder/.cache/hermes-webui-src
PROXY_SCRIPT=/tmp/coder-mounted-proxy.mjs
WEBAPP_FRONT=/home/coder/.local/share/dokploy/hermes-webapp-front.mjs
BOOTSTRAP_PID_FILE=/tmp/hermes-workspace-bootstrap.pid
STATE_DIR=/tmp/hermes-webui-watchdog
RESTART_COOLDOWN_SECONDS=120
SUPERVISE_INTERVAL_SECONDS=60

# Cron and supervise loops get neither the rc files nor the agent environment.
PATH="\$PATH:/home/coder/.local/bin:/usr/local/bin"

mkdir -p "\$STATE_DIR"
log() { printf '%s %s\n' "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "\$*"; }

healthy() {
  curl -fsS --max-time 5 "http://127.0.0.1:\$1/health" >/dev/null 2>&1
}

healthy_webapp() {
  # The tile port is the whole chain the browser traverses: front and webapp listener.
  # /api/health answers unsigned on the listener and the front passes it through, so
  # this fails when either half dies.
  curl -fsS --max-time 5 "http://127.0.0.1:\$HERMES_WEBAPP_PORT\$HERMES_WEBAPP_HEALTH_PATH" \
    >/dev/null 2>&1
}

healthy_webapp_backend() {
  curl -fsS --max-time 5 "http://127.0.0.1:\$HERMES_WEBAPP_INTERNAL_PORT\$HERMES_WEBAPP_HEALTH_PATH" \
    >/dev/null 2>&1
}

bootstrapping() {
  # Cold starts clone and npm-install the WebUI for minutes. A port that is not
  # answering yet is normal then, so never fight a bootstrap that is still running.
  [ -f "\$BOOTSTRAP_PID_FILE" ] || return 1
  bootstrap_pid=\$(cat "\$BOOTSTRAP_PID_FILE" 2>/dev/null || true)
  [ -n "\$bootstrap_pid" ] && kill -0 "\$bootstrap_pid" 2>/dev/null
}

took_cooldown() {
  # One restart per service per cooldown window, so a target that refuses to come
  # back up cannot spin the box in a restart loop.
  now=\$(date +%s)
  last=\$(cat "\$STATE_DIR/\$1" 2>/dev/null || echo 0)
  if [ \$((now - last)) -lt \$RESTART_COOLDOWN_SECONDS ]; then
    return 0
  fi
  printf '%s' "\$now" > "\$STATE_DIR/\$1"
  return 1
}

start_gateway() {
  log "reviving hermes gateway"
  nohup hermes gateway >>/tmp/hermes-gateway.log 2>&1 &
}

start_webui() {
  log "reviving hermes webui on port \$HERMES_WEBUI_PORT"
  nohup env HERMES_HOME="\$HERMES_HOME" HERMES_WEBUI_ALLOWED_ORIGINS="\$HERMES_PUBLIC_ORIGIN" HERMES_WEBUI_TRUST_FORWARDED_HOST=1 HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=\$HERMES_WEBUI_PORT HERMES_WEBUI_AGENT_DIR=\$HERMES_INSTALL_DIR HERMES_WEBUI_STATE_DIR="\$HERMES_HOME/webui" HERMES_WEBUI_SKIP_ONBOARDING=1 HERMES_WEBUI_DEFAULT_MODEL="\$HERMES_MODEL" PYTHONPATH="\$HERMES_INSTALL_DIR" python3 "\$WEBUI_SRC/bootstrap.py" --no-browser --skip-agent-install >>/tmp/hermes-webui.log 2>&1 &
}

start_webapp_front() {
  log "reviving Desktop webapp front on port \$HERMES_WEBAPP_PORT"
  nohup env HERMES_HOME="\$HERMES_HOME" HERMES_WEBAPP_INTERNAL_PORT="\$HERMES_WEBAPP_INTERNAL_PORT" PROXY_PORT="\$HERMES_WEBAPP_PORT" node "\$WEBAPP_FRONT" >>/tmp/hermes-webapp-front.log 2>&1 &
}

restart_gateway_for_webapp() {
  log "restarting hermes gateway so it serves the Desktop webapp on port \$HERMES_WEBAPP_INTERNAL_PORT"
  # The Webapp rides the gateway listener and its config is read only at start, so the
  # gateway's own restart is the fix. The front is stateless and starts beside it:
  # answering 502 while the listener warms up is what the front is for.
  nohup hermes gateway restart >>/tmp/hermes-gateway-restart.log 2>&1 &
  start_webapp_front
}

start_proxy() {
  log "reviving coder mounted proxy on port \$HERMES_WEBUI_PROXY_PORT"
  nohup env TARGET_PORT=\$HERMES_WEBUI_PORT PROXY_PORT=\$HERMES_WEBUI_PROXY_PORT node "\$PROXY_SCRIPT" >>/tmp/hermes-webui-proxy.log 2>&1 &
}

run_checks() {
  bootstrapping && return 0
  if ! healthy "\$API_SERVER_PORT" && ! took_cooldown gateway; then
    pkill -f "hermes gateway" >/dev/null 2>&1 || true
    start_gateway
  fi
  if ! healthy "\$HERMES_WEBUI_PORT" && ! took_cooldown webui; then
    pkill -f "hermes-webui-src/bootstrap.py" >/dev/null 2>&1 || true
    start_webui
  fi
  # Only when the gateway itself is answering: if its API port is down, the gateway
  # branch above already owns bringing it back, and restarting from here too would
  # start the shared gateway twice.
  if ! healthy_webapp && healthy "\$API_SERVER_PORT" && ! took_cooldown webapp; then
    if healthy_webapp_backend; then
      # Only the front died: the listener answers on its internal port, and restarting
      # the gateway would drop every session the webapp holds for no reason.
      pkill -f "hermes-webapp-front[.]mjs" >/dev/null 2>&1 || true
      start_webapp_front
    else
      restart_gateway_for_webapp
    fi
  fi
  # The proxy answers /health only by forwarding upstream, so judge it by process.
  if [ -f "\$PROXY_SCRIPT" ] && ! pgrep -f "node \$PROXY_SCRIPT" >/dev/null 2>&1 \
    && ! took_cooldown proxy; then
    start_proxy
  fi
}

case "\$*" in
  --supervise)
    while :; do
      run_checks
      sleep "\$SUPERVISE_INTERVAL_SECONDS"
    done
    ;;
esac

run_checks
WDSH
chmod 700 "$HERMES_WATCHDOG_SCRIPT"

if command -v crontab >/dev/null 2>&1; then
  { crontab -l 2>/dev/null | grep -vF "hermes-webui-watchdog.sh"
    printf '* * * * * %s >>/tmp/hermes-webui-watchdog.log 2>&1\n' "$HERMES_WATCHDOG_SCRIPT"; } | crontab - \
    || echo "cron entry not installed; the supervise loop covers this workspace" >&2
fi

if ! pgrep -f "hermes-webui-watchdog.sh --supervise" >/dev/null 2>&1; then
  setsid nohup "$HERMES_WATCHDOG_SCRIPT" --supervise >>/tmp/hermes-webui-watchdog.log 2>&1 < /dev/null &
fi
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

resource "coder_app" "opencode" {
  agent_id     = coder_agent.main.id
  slug         = "opencode"
  display_name = "OpenCode"
  icon         = "https://raw.githubusercontent.com/anomalyco/opencode/refs/heads/dev/packages/ui/src/assets/favicon/favicon-v3.svg"
  # Subdomain app: opencode serve v2 has no base-path support, so it must be
  # served at an origin root (via the coder wildcard host) rather than behind
  # a mounted-path proxy.
  url          = "http://localhost:4096"
  share        = "owner"
  subdomain    = true
  order        = 2

  healthcheck {
    url       = "http://localhost:4096"
    interval  = 5
    threshold = 12
  }
}

resource "coder_app" "hermes_desktop" {
  agent_id     = coder_agent.main.id
  slug         = local.hermes_desktop_slug
  display_name = "Hermes Desktop (Web)"
  icon         = "https://raw.githubusercontent.com/NousResearch/hermes-agent/refs/heads/main/apps/desktop/assets/icon.png"
  # Subdomain app: the Desktop renderer is a root-path SPA with no base-path support,
  # so it rides the Coder wildcard route rather than a /apps/<slug> mount. The port
  # named here is the loopback front, never the webapp listener itself.
  url          = "http://localhost:9120"
  share        = "owner"
  subdomain    = true
  # "tab" is one of the three words the provider accepts (window, tab, slim-window);
  # anything else fails the whole plan. Opening in Coder's frame would nest a sign-in
  # form inside a frame that redirects, so the tile hands the browser a real tab.
  open_in      = "tab"
  order        = 3

  healthcheck {
    # /api/health is the endpoint the webapp answers without a session; the request
    # reaches it through the loopback front, so a dead front or a dead listener both
    # read unhealthy here, and only a live chain reads healthy.
    url       = "http://localhost:9120/api/health"
    interval  = 5
    threshold = 60
  }
}

resource "coder_app" "hermes_webui" {
  agent_id     = coder_agent.main.id
  slug         = "hermes-webui"
  display_name = "Hermes WebUI (Community)"
  icon         = "https://raw.githubusercontent.com/nesquena/hermes-webui/refs/heads/master/static/favicon.svg"
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
