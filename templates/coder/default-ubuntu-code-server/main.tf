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

    # Preseed OpenCode and Pi with full LiteLLM alias IDs so selected models match
    # the wizard-managed proxy allowlists exactly.
    export AI_DEFAULT_PROVIDER="$${AI_DEFAULT_PROVIDER:-__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__}"
    export AI_DEFAULT_MODEL="$${AI_DEFAULT_MODEL:-__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__}"
    export AI_DEFAULT_BASE_URL="$${AI_DEFAULT_BASE_URL:-__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__}"
    export AI_DEFAULT_API_KEY="$${AI_DEFAULT_API_KEY:-__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__}"
    export OPENCODE_GO_BASE_URL="$${OPENCODE_GO_BASE_URL:-$AI_DEFAULT_BASE_URL}"
    export OPENCODE_GO_API_KEY="$${OPENCODE_GO_API_KEY:-$AI_DEFAULT_API_KEY}"
    export LITELLM_DEFAULT_ALIAS="$AI_DEFAULT_PROVIDER/$AI_DEFAULT_MODEL"
    export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"

    mkdir -p /home/coder/.config/opencode /home/coder/.pi/agent

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


def fetch_model_ids() -> list[str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{base_url}/v1/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return []

    model_ids: list[str] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str):
            continue
        normalized = model_id.strip()
        if (
            normalized
            and "/" in normalized
            and not normalized.endswith("/*")
            and not normalized.startswith("openai/")
        ):
            model_ids.append(normalized)
    return model_ids


model_ids = list(dict.fromkeys(fetch_model_ids() + fallback_models))
if default_alias not in model_ids:
    model_ids.insert(0, default_alias)

opencode_config = {
    "provider": {
        "litellm": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": base_url, "apiKey": api_key},
            "models": {model_id: {} for model_id in model_ids},
        }
    },
    "model": default_alias,
}
Path("/home/coder/.config/opencode/opencode.json").write_text(
    json.dumps(opencode_config, indent=2) + "\n",
    encoding="utf-8",
)

pi_models_config = {
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
    json.dumps(pi_models_config, indent=2) + "\n",
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
