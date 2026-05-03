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

  kdense_model_options = [
    {
      name  = "Claude Opus 4.7"
      value = "openrouter/anthropic/claude-opus-4.7"
      icon  = "/emojis/1f9e0.png"
    },
    {
      name  = "Claude Sonnet 4.6"
      value = "openrouter/anthropic/claude-sonnet-4.6"
      icon  = "/emojis/1f9e0.png"
    },
    {
      name  = "GPT-5.4 Pro"
      value = "openrouter/openai/gpt-5.4-pro"
      icon  = "/emojis/1f916.png"
    },
    {
      name  = "GPT-5.4"
      value = "openrouter/openai/gpt-5.4"
      icon  = "/emojis/1f916.png"
    },
    {
      name  = "GPT-5.4 Mini"
      value = "openrouter/openai/gpt-5.4-mini"
      icon  = "/emojis/1f916.png"
    },
    {
      name  = "GPT-5.4 Nano"
      value = "openrouter/openai/gpt-5.4-nano"
      icon  = "/emojis/1f916.png"
    },
    {
      name  = "Grok 4.20 Beta"
      value = "openrouter/x-ai/grok-4.20-beta"
      icon  = "/emojis/1f680.png"
    },
    {
      name  = "Gemini 3.1 Pro Preview"
      value = "openrouter/google/gemini-3.1-pro-preview"
      icon  = "/emojis/2728.png"
    },
    {
      name  = "Gemini 3 Flash Preview"
      value = "openrouter/google/gemini-3-flash-preview"
      icon  = "/emojis/2728.png"
    },
    {
      name  = "Gemini 3.1 Flash Lite Preview"
      value = "openrouter/google/gemini-3.1-flash-lite-preview"
      icon  = "/emojis/2728.png"
    },
    {
      name  = "Qwen3 Max Thinking"
      value = "openrouter/qwen/qwen3-max-thinking"
      icon  = "/emojis/1f4a1.png"
    },
    {
      name  = "Qwen3 Coder Next"
      value = "openrouter/qwen/qwen3-coder-next"
      icon  = "/emojis/1f4bb.png"
    },
    {
      name  = "GLM 5 Turbo"
      value = "openrouter/z-ai/glm-5-turbo"
      icon  = "/emojis/1f3af.png"
    },
    {
      name  = "GLM 5"
      value = "openrouter/z-ai/glm-5"
      icon  = "/emojis/1f3af.png"
    },
    {
      name  = "MiniMax M2.5"
      value = "openrouter/minimax/minimax-m2.5"
      icon  = "/emojis/1f4ca.png"
    },
    {
      name  = "MiniMax M2.5 (free)"
      value = "openrouter/minimax/minimax-m2.5:free"
      icon  = "/emojis/1f4ca.png"
    },
    {
      name  = "Kimi K2.5"
      value = "openrouter/moonshotai/kimi-k2.5"
      icon  = "/emojis/1f311.png"
    },
    {
      name  = "Nemotron 3 Super"
      value = "openrouter/nvidia/nemotron-3-super-120b-a12b"
      icon  = "/emojis/1f9ee.png"
    },
    {
      name  = "Nemotron 3 Nano Omni (free)"
      value = "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
      icon  = "/emojis/1f9ee.png"
    },
  ]
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "kdense_provider" {
  name         = "kdense_provider"
  display_name = "K-Dense Provider"
  description  = "Use the normal upstream OpenRouter path or the extra OpenCode Go-compatible endpoint."
  icon         = "/emojis/1f310.png"
  type         = "string"
  mutable      = true
  default      = "openrouter"

  option {
    name  = "OpenRouter"
    value = "openrouter"
    icon  = "/emojis/1f310.png"
  }

  option {
    name  = "OpenCode Go"
    value = "opencode_go"
    icon  = "/icon/code.svg"
  }
}

data "coder_parameter" "kdense_default_model" {
  name         = "kdense_default_model"
  display_name = "Default Chat Model"
  description  = "Choose the default Kady model. When the provider is OpenCode Go, the selected model is mapped onto the OpenCode Go-compatible endpoint automatically."
  icon         = "/emojis/1f9e0.png"
  type         = "string"
  mutable      = true
  default      = "openrouter/anthropic/claude-opus-4.7"

  dynamic "option" {
    for_each = local.kdense_model_options
    content {
      name  = option.value.name
      value = option.value.value
      icon  = option.value.icon
    }
  }
}

data "coder_parameter" "kdense_expert_model" {
  name         = "kdense_expert_model"
  display_name = "Default Expert Model"
  description  = "Choose the default delegated expert model. When the provider is OpenCode Go, the selected model is mapped onto the OpenCode Go-compatible endpoint automatically."
  icon         = "/emojis/1f52c.png"
  type         = "string"
  mutable      = true
  default      = "openrouter/google/gemini-3.1-pro-preview"

  dynamic "option" {
    for_each = local.kdense_model_options
    content {
      name  = option.value.name
      value = option.value.value
      icon  = option.value.icon
    }
  }
}

data "coder_parameter" "kdense_search_provider" {
  name         = "kdense_search_provider"
  display_name = "Search Provider"
  description  = "Optional web search provider to enable in the workspace."
  icon         = "/emojis/1f50d.png"
  type         = "string"
  mutable      = true
  default      = "disabled"

  option {
    name  = "Disabled"
    value = "disabled"
    icon  = "/emojis/26aa.png"
  }

  option {
    name  = "Exa"
    value = "exa"
    icon  = "/emojis/1f30e.png"
  }

  option {
    name  = "Parallel"
    value = "parallel"
    icon  = "/emojis/1f9ed.png"
  }
}

data "coder_parameter" "kdense_openrouter_api_key" {
  name         = "kdense_openrouter_api_key"
  display_name = "OpenRouter API Key"
  description  = "Optional workspace-level OpenRouter key. Leave blank to rely on other configured provider paths."
  icon         = "/emojis/1f511.png"
  type         = "string"
  mutable      = true
  default      = ""
}

data "coder_parameter" "kdense_openrouter_base_url" {
  name         = "kdense_openrouter_base_url"
  display_name = "OpenRouter Base URL"
  description  = "Override the OpenRouter-compatible base URL if needed."
  icon         = "/emojis/1f517.png"
  type         = "string"
  mutable      = true
  default      = "https://openrouter.ai/api/v1"
}

data "coder_parameter" "kdense_opencode_go_api_key" {
  name         = "kdense_opencode_go_api_key"
  display_name = "OpenCode Go API Key"
  description  = "Optional override for the central LiteLLM OpenCode Go-compatible wildcard key. Leave blank to use the wizard-managed K-Dense gateway key."
  icon         = "/emojis/1f511.png"
  type         = "string"
  mutable      = true
  default      = ""
}

data "coder_parameter" "kdense_opencode_go_base_url" {
  name         = "kdense_opencode_go_base_url"
  display_name = "OpenCode Go Base URL"
  description  = "Optional override for the central LiteLLM OpenCode Go-compatible wildcard base URL. Leave blank to use the wizard-managed internal gateway URL."
  icon         = "/emojis/1f517.png"
  type         = "string"
  mutable      = true
  default      = "https://opencode.ai/zen/go/v1"
}

data "coder_parameter" "kdense_exa_api_key" {
  name         = "kdense_exa_api_key"
  display_name = "Exa API Key"
  description  = "Optional Exa web search key. Used only when Search Provider is set to Exa."
  icon         = "/emojis/1f511.png"
  type         = "string"
  mutable      = true
  default      = ""
}

data "coder_parameter" "kdense_parallel_api_key" {
  name         = "kdense_parallel_api_key"
  display_name = "Parallel API Key"
  description  = "Optional Parallel search key. Used only when Search Provider is set to Parallel."
  icon         = "/emojis/1f511.png"
  type         = "string"
  mutable      = true
  default      = ""
}

data "coder_parameter" "kdense_modal_token_id" {
  name         = "kdense_modal_token_id"
  display_name = "Modal Token ID"
  description  = "Optional Modal token ID for remote compute."
  icon         = "/emojis/1f511.png"
  type         = "string"
  mutable      = true
  default      = ""
}

data "coder_parameter" "kdense_modal_token_secret" {
  name         = "kdense_modal_token_secret"
  display_name = "Modal Token Secret"
  description  = "Optional Modal token secret for remote compute."
  icon         = "/emojis/1f511.png"
  type         = "string"
  mutable      = true
  default      = ""
}

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"
  dir  = "/home/coder"
}

resource "coder_script" "kdense_bootstrap" {
  agent_id           = coder_agent.main.id
  display_name       = "K-Dense BYOK Bootstrap"
  icon               = "/icon/code.svg"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600

  script = <<-EOT
    cat >/tmp/kdense-bootstrap.sh <<'BOOT'
    set -euo pipefail

    _SUDO=""
    if command -v sudo >/dev/null 2>&1; then
      _SUDO="sudo"
    fi

    $_SUDO apt-get update -q
    missing_packages=()
    for package in curl ca-certificates wget python3; do
      if ! dpkg -s "$package" >/dev/null 2>&1; then
        missing_packages+=("$package")
      fi
    done
    if ! command -v git >/dev/null 2>&1; then
      missing_packages+=("git")
    fi
    if ! command -v btop >/dev/null 2>&1; then
      missing_packages+=("btop")
    fi
    if [ "$${#missing_packages[@]}" -gt 0 ]; then
      $_SUDO apt-get install -y "$${missing_packages[@]}"
    fi

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

    if ! command -v zellij >/dev/null 2>&1; then
      ARCH=$(uname -m)
      ZELLIJ_URL="https://github.com/zellij-org/zellij/releases/latest/download/zellij-$${ARCH}-unknown-linux-musl.tar.gz"
      curl -fsSL "$${ZELLIJ_URL}" | $_SUDO tar -C /usr/local/bin -xz
    fi

    NEED_NODE=true
    if command -v node >/dev/null 2>&1; then
      NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
      if [ "$NODE_MAJOR" -ge 22 ]; then
        NEED_NODE=false
      fi
    fi
    if [ "$NEED_NODE" = true ]; then
      case "$(uname -m)" in
        x86_64) NODE_DIST_ARCH=x64 ;;
        aarch64|arm64) NODE_DIST_ARCH=arm64 ;;
        *)
          echo "Unsupported architecture for Node.js install: $(uname -m)" >&2
          exit 1
          ;;
      esac
      NODE_TARBALL=$(NODE_DIST_ARCH="$NODE_DIST_ARCH" python3 - <<'PY'
import os
import urllib.request

arch = os.environ["NODE_DIST_ARCH"]
with urllib.request.urlopen("https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt", timeout=30) as response:
    text = response.read().decode("utf-8", "replace")

for line in text.splitlines():
    parts = line.split()
    if len(parts) == 2 and parts[1].endswith(f"linux-{arch}.tar.xz"):
        print(parts[1])
        break
PY
)
      if [ -z "$NODE_TARBALL" ]; then
        echo "Unable to determine latest Node.js 22 tarball for $NODE_DIST_ARCH" >&2
        exit 1
      fi
      curl -fsSL "https://nodejs.org/dist/latest-v22.x/$NODE_TARBALL" | $_SUDO tar -xJf - -C /usr/local --strip-components=1 --no-same-owner
    fi

    export PATH="/home/coder/.local/bin:/home/coder/.cargo/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="/home/coder/.local/bin:/home/coder/.cargo/bin:$PATH"
    fi

    if ! command -v gemini >/dev/null 2>&1; then
      NPM_CONFIG_PREFIX=/home/coder/.local npm install -g @google/gemini-cli
    fi

    export KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL="__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__"
    export KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY="__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__"

    KDENSE_PROVIDER="$${KDENSE_PROVIDER:-openrouter}"
    KDENSE_DEFAULT_MODEL="$${KDENSE_DEFAULT_MODEL:-openrouter/anthropic/claude-opus-4.7}"
    KDENSE_EXPERT_MODEL="$${KDENSE_EXPERT_MODEL:-openrouter/google/gemini-3.1-pro-preview}"
    KDENSE_SEARCH_PROVIDER="$${KDENSE_SEARCH_PROVIDER:-disabled}"

    KDENSE_OPENROUTER_API_KEY="$${KDENSE_OPENROUTER_API_KEY:-}"
    KDENSE_OPENROUTER_BASE_URL="$${KDENSE_OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
    # Central LiteLLM gateway owns the OpenCode Go wildcard route.
    KDENSE_CENTRAL_LITELLM_API_KEY="$${KDENSE_OPENCODE_GO_API_KEY:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_API_KEY}"
    KDENSE_CENTRAL_LITELLM_BASE_URL="$${KDENSE_OPENCODE_GO_BASE_URL:-$KDENSE_TEMPLATE_LITELLM_GATEWAY_BASE_URL}"
    KDENSE_EXA_API_KEY="$${KDENSE_EXA_API_KEY:-}"
    KDENSE_PARALLEL_API_KEY="$${KDENSE_PARALLEL_API_KEY:-}"
    KDENSE_MODAL_TOKEN_ID="$${KDENSE_MODAL_TOKEN_ID:-}"
    KDENSE_MODAL_TOKEN_SECRET="$${KDENSE_MODAL_TOKEN_SECRET:-}"

    case "$KDENSE_PROVIDER" in
      openrouter|opencode_go) ;;
      *)
        echo "Unsupported KDENSE_PROVIDER: $KDENSE_PROVIDER" >&2
        exit 1
        ;;
    esac

    case "$KDENSE_SEARCH_PROVIDER" in
      disabled|exa|parallel) ;;
      *)
        echo "Unsupported KDENSE_SEARCH_PROVIDER: $KDENSE_SEARCH_PROVIDER" >&2
        exit 1
        ;;
    esac

    if [ -n "$KDENSE_MODAL_TOKEN_ID" ] || [ -n "$KDENSE_MODAL_TOKEN_SECRET" ]; then
      if [ -z "$KDENSE_MODAL_TOKEN_ID" ] || [ -z "$KDENSE_MODAL_TOKEN_SECRET" ]; then
        echo "Both KDENSE_MODAL_TOKEN_ID and KDENSE_MODAL_TOKEN_SECRET are required together." >&2
        exit 1
      fi
    fi

    if [ "$KDENSE_SEARCH_PROVIDER" = "exa" ] && [ -z "$KDENSE_EXA_API_KEY" ]; then
      echo "KDENSE_EXA_API_KEY is required when KDENSE_SEARCH_PROVIDER=exa." >&2
      exit 1
    fi

    if [ "$KDENSE_SEARCH_PROVIDER" = "parallel" ] && [ -z "$KDENSE_PARALLEL_API_KEY" ]; then
      echo "KDENSE_PARALLEL_API_KEY is required when KDENSE_SEARCH_PROVIDER=parallel." >&2
      exit 1
    fi

    if [ "$KDENSE_PROVIDER" = "openrouter" ] && [ -z "$KDENSE_OPENROUTER_API_KEY" ] && [ -n "$KDENSE_CENTRAL_LITELLM_API_KEY" ]; then
      echo "K-Dense: no OpenRouter key provided; falling back to the wizard-managed OpenCode Go provider." >&2
      KDENSE_PROVIDER="opencode_go"
    fi

    if [ "$KDENSE_PROVIDER" = "openrouter" ] && [ -z "$KDENSE_OPENROUTER_API_KEY" ]; then
      echo "KDENSE_OPENROUTER_API_KEY is required when using the OpenRouter provider." >&2
      exit 1
    fi

    if [ "$KDENSE_PROVIDER" = "opencode_go" ] && [ -z "$KDENSE_CENTRAL_LITELLM_API_KEY" ]; then
      echo "KDENSE_CENTRAL_LITELLM_API_KEY is required when using the OpenCode Go provider." >&2
      exit 1
    fi

    normalize_model_for_provider() {
      provider="$1"
      model="$2"
      case "$provider" in
        openrouter)
          case "$model" in
            openrouter/*) printf '%s' "$model" ;;
            openai/*) printf 'openrouter/%s' "$${model#openai/}" ;;
            *) printf '%s' "$model" ;;
          esac
          ;;
        opencode_go)
          case "$model" in
            openrouter/*) printf 'openai/%s' "$${model#openrouter/}" ;;
            openai/*) printf '%s' "$model" ;;
            *) printf '%s' "$model" ;;
          esac
          ;;
      esac
    }

    KDENSE_DEFAULT_MODEL_EFFECTIVE=$(normalize_model_for_provider "$KDENSE_PROVIDER" "$KDENSE_DEFAULT_MODEL")
    KDENSE_EXPERT_MODEL_EFFECTIVE=$(normalize_model_for_provider "$KDENSE_PROVIDER" "$KDENSE_EXPERT_MODEL")

    KDENSE_SRC_DIR=/home/coder/.cache/kdense-byok-src
    KDENSE_SETUP_STAMP=/home/coder/.cache/kdense-byok-setup-rev
    KDENSE_SETUP_KEY=v10-upstream-main-parameterized
    KDENSE_UI_PORT=3000
    KDENSE_API_PORT=8000
    KDENSE_LITELLM_PORT=4000
    # Workspace-local LiteLLM stays on localhost for the Gemini/OpenAI shim only.
    KDENSE_LOCAL_LITELLM_BASE_URL="http://localhost:$KDENSE_LITELLM_PORT"
    KDENSE_PROXY_PORT=3001
    KDENSE_NEEDS_PREP=false

    mkdir -p /home/coder/.cache /home/coder/.local/bin

    sync_kdense_source() {
      EXPECTED_REPO_URL="https://github.com/K-Dense-AI/k-dense-byok.git"
      if [ -d "$KDENSE_SRC_DIR/.git" ]; then
        CURRENT_REPO_URL=$(git -C "$KDENSE_SRC_DIR" remote get-url origin 2>/dev/null || true)
        if [ "$CURRENT_REPO_URL" != "$EXPECTED_REPO_URL" ]; then
          rm -rf "$KDENSE_SRC_DIR"
        fi
      fi

      if [ -d "$KDENSE_SRC_DIR/.git" ]; then
        for attempt in 1 2 3; do
          if git -C "$KDENSE_SRC_DIR" fetch --depth 1 origin main \
            && git -C "$KDENSE_SRC_DIR" checkout -f main \
            && git -C "$KDENSE_SRC_DIR" reset --hard origin/main; then
            return 0
          fi
          sleep 5
        done
      else
        for attempt in 1 2 3; do
          rm -rf "$KDENSE_SRC_DIR"
          if git clone --depth 1 --branch main https://github.com/K-Dense-AI/k-dense-byok.git "$KDENSE_SRC_DIR"; then
            return 0
          fi
          sleep 5
        done
      fi

      rm -rf "$KDENSE_SRC_DIR"
      mkdir -p "$KDENSE_SRC_DIR"
      if curl -fsSL https://codeload.github.com/K-Dense-AI/k-dense-byok/tar.gz/refs/heads/main | tar -xz --strip-components=1 -C "$KDENSE_SRC_DIR"; then
        return 0
      fi

      echo "Unable to fetch K-Dense BYOK source from GitHub" >&2
      exit 1
    }

    sync_kdense_source

    python3 - <<'PY' "$KDENSE_SRC_DIR/web/src/components/ai-elements/message.tsx"
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = 'const streamdownComponents = { p: SafeParagraph } as ComponentProps<typeof Streamdown>["components"];'
new = 'const streamdownComponents = { p: SafeParagraph } as unknown as ComponentProps<typeof Streamdown>["components"];'
if old in text:
    text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
PY

    python3 - <<'PY' "$KDENSE_SRC_DIR/web/src/lib/use-agent.ts"
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = re.sub(r'status:\s*"running",', 'status: "running" as const,', text, count=1)
path.write_text(text, encoding="utf-8")
PY

    python3 - <<'PY' "$KDENSE_SRC_DIR/web/vitest.setup.ts"
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace('// @ts-expect-error polyfill\n', '')
path.write_text(text, encoding="utf-8")
PY

    append_env() {
      key="$1"
      value="$2"
      env_file="$3"
      if [ -n "$value" ]; then
        printf '%s=%s\n' "$key" "$value" >> "$env_file"
      fi
    }

    write_kdense_env_file() {
      env_file="$1"
      : > "$env_file"
      printf 'GOOGLE_GEMINI_BASE_URL=%s\n' "$KDENSE_LOCAL_LITELLM_BASE_URL" >> "$env_file"
      printf 'GEMINI_API_KEY=sk-litellm-local\n' >> "$env_file"
      printf 'DEFAULT_AGENT_MODEL=%s\n' "$KDENSE_DEFAULT_MODEL_EFFECTIVE" >> "$env_file"
      printf 'DEFAULT_EXPERT_MODEL=%s\n' "$KDENSE_EXPERT_MODEL_EFFECTIVE" >> "$env_file"
      append_env OPENROUTER_API_KEY "$KDENSE_OPENROUTER_API_KEY" "$env_file"
      append_env OPENAI_API_KEY "$KDENSE_CENTRAL_LITELLM_API_KEY" "$env_file"
      append_env OPENAI_API_BASE "$KDENSE_CENTRAL_LITELLM_BASE_URL" "$env_file"
      append_env OPENAI_BASE_URL "$KDENSE_CENTRAL_LITELLM_BASE_URL" "$env_file"
      append_env EXA_API_KEY "$KDENSE_EXA_API_KEY" "$env_file"
      append_env PARALLEL_API_KEY "$KDENSE_PARALLEL_API_KEY" "$env_file"
      append_env MODAL_TOKEN_ID "$KDENSE_MODAL_TOKEN_ID" "$env_file"
      append_env MODAL_TOKEN_SECRET "$KDENSE_MODAL_TOKEN_SECRET" "$env_file"
      chmod 600 "$env_file"
    }

    KDENSE_ROOT_ENV_FILE="$KDENSE_SRC_DIR/.env"
    KDENSE_AGENT_ENV_FILE="$KDENSE_SRC_DIR/kady_agent/.env"
    write_kdense_env_file "$KDENSE_ROOT_ENV_FILE"
    write_kdense_env_file "$KDENSE_AGENT_ENV_FILE"

    KDENSE_SEARCH_PROVIDER_EFFECTIVE="$KDENSE_SEARCH_PROVIDER"
    if [ "$KDENSE_SEARCH_PROVIDER_EFFECTIVE" = "disabled" ]; then
      KDENSE_EXA_API_KEY=""
      KDENSE_PARALLEL_API_KEY=""
    elif [ "$KDENSE_SEARCH_PROVIDER_EFFECTIVE" = "exa" ]; then
      KDENSE_PARALLEL_API_KEY=""
    elif [ "$KDENSE_SEARCH_PROVIDER_EFFECTIVE" = "parallel" ]; then
      KDENSE_EXA_API_KEY=""
    fi

    write_kdense_env_file "$KDENSE_ROOT_ENV_FILE"
    write_kdense_env_file "$KDENSE_AGENT_ENV_FILE"

    KDENSE_UPSTREAM_LITELLM="$KDENSE_SRC_DIR/litellm_config.yaml"
    python3 - <<'PY' "$KDENSE_UPSTREAM_LITELLM"
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
block = '''
  # OpenRouter stays explicit-model only; the central LiteLLM gateway owns the OpenCode Go wildcard route.
  # ── OpenCode Go via OpenAI-compatible endpoint ──
  - model_name: "openai/*"
    litellm_params:
      model: "openai/*"
      api_base: os.environ/OPENAI_API_BASE
      api_key: os.environ/OPENAI_API_KEY
      timeout: 600
      stream_timeout: 600
      extra_headers:
        HTTP-Referer: "https://www.k-dense.ai"
        X-Title: "Kady-Expert"

'''
needle = '  # ── Local Ollama (no API key required) ──\n'
if 'model_name: "openai/*"' not in text and needle in text:
    text = text.replace(needle, block + needle)
    path.write_text(text, encoding="utf-8")
PY

    KDENSE_MODELS_JSON="$KDENSE_SRC_DIR/web/src/data/models.json"
    python3 - <<'PY' "$KDENSE_MODELS_JSON" "$KDENSE_DEFAULT_MODEL_EFFECTIVE" "$KDENSE_EXPERT_MODEL_EFFECTIVE" "$KDENSE_OPENROUTER_API_KEY" "$KDENSE_CENTRAL_LITELLM_API_KEY"
from pathlib import Path
import json
import sys

path = Path(sys.argv[1])
default_model = sys.argv[2]
expert_model = sys.argv[3]
openrouter_api_key = sys.argv[4]
opencode_go_api_key = sys.argv[5]

models = json.loads(path.read_text(encoding="utf-8"))

def clear_flags(model: dict) -> dict:
    next_model = dict(model)
    next_model.pop("default", None)
    next_model.pop("expertDefault", None)
    return next_model

openrouter_models = [clear_flags(model) for model in models if str(model.get("id", "")).startswith("openrouter/")]
merged = []
if openrouter_api_key:
    merged.extend(openrouter_models)
if opencode_go_api_key:
    for model in openrouter_models:
      clone = dict(model)
      clone["id"] = "openai/" + str(model["id"])[len("openrouter/"):]
      clone["provider"] = "OpenCode Go"
      description = str(model.get("description", "")).strip()
      clone["description"] = (description + "\n\n") if description else ""
      clone["description"] += "Available through the central LiteLLM OpenCode Go-compatible gateway."
      merged.append(clone)
    merged.append(
        {
            "id": "openai/deepseek-v4-flash",
            "label": "DeepSeek V4 Flash",
            "provider": "OpenCode Go",
            "tier": "mid",
            "context_length": 262144,
            "pricing": {"prompt": 0.0, "completion": 0.0},
            "modality": "text->text",
            "description": "Available through the central LiteLLM OpenCode Go-compatible gateway.",
        }
    )

seen = set()
deduped = []
for model in merged:
    model_id = str(model.get("id", ""))
    if not model_id or model_id in seen:
        continue
    seen.add(model_id)
    if model_id == default_model:
        model["default"] = True
    if model_id == expert_model:
        model["expertDefault"] = True
    deduped.append(model)

if deduped and not any(model.get("default") for model in deduped):
    deduped[0]["default"] = True
if deduped and not any(model.get("expertDefault") for model in deduped):
    fallback = next((model for model in deduped if model.get("default")), deduped[0])
    fallback["expertDefault"] = True

path.write_text(json.dumps(deduped, indent=2) + "\n", encoding="utf-8")
PY

    if [ -d "$KDENSE_SRC_DIR/.git" ]; then
      KDENSE_REV=$(git -C "$KDENSE_SRC_DIR" rev-parse HEAD)
    else
      KDENSE_REV=archive-main
    fi
    KDENSE_SETUP_ID="$KDENSE_REV:$KDENSE_SETUP_KEY:$KDENSE_PROVIDER:$KDENSE_DEFAULT_MODEL_EFFECTIVE:$KDENSE_EXPERT_MODEL_EFFECTIVE"

    if [ ! -d "$KDENSE_SRC_DIR/.venv" ] || [ ! -d "$KDENSE_SRC_DIR/web/node_modules" ] || [ ! -f "$KDENSE_SRC_DIR/web/.next/BUILD_ID" ] || [ ! -f "$KDENSE_SETUP_STAMP" ] || [ "$(cat "$KDENSE_SETUP_STAMP" 2>/dev/null || true)" != "$KDENSE_SETUP_ID" ]; then
      cd "$KDENSE_SRC_DIR"
      uv python install 3.13
      uv sync --python 3.13 --no-dev --quiet
      if [ -f web/package-lock.json ]; then
        (cd web && NEXT_PUBLIC_ADK_API_URL= npm ci --silent && NEXT_PUBLIC_ADK_API_URL= npm run build)
      else
        (cd web && NEXT_PUBLIC_ADK_API_URL= npm install --silent && NEXT_PUBLIC_ADK_API_URL= npm run build)
      fi
      printf '%s' "$KDENSE_SETUP_ID" > "$KDENSE_SETUP_STAMP"
    fi
    if [ ! -d "$KDENSE_SRC_DIR/sandbox/.gemini/skills" ]; then
      KDENSE_NEEDS_PREP=true
    fi

    wait_for_http() {
      url="$1"
      attempts="$2"
      for _ in $(seq 1 "$attempts"); do
        if curl -fsS "$url" >/dev/null 2>&1; then
          return 0
        fi
        sleep 2
      done
      return 1
    }

    cat >/tmp/coder-mounted-proxy.mjs <<'JS'
import http from "node:http";
import net from "node:net";

const UI_HOST = process.env.UI_HOST || "127.0.0.1";
const UI_PORT = Number(process.env.UI_PORT || "0");
const API_HOST = process.env.API_HOST || "127.0.0.1";
const API_PORT = Number(process.env.API_PORT || "0");
const PROXY_PORT = Number(process.env.PROXY_PORT || "0");

if (!UI_PORT || !API_PORT || !PROXY_PORT) {
  throw new Error("UI_PORT, API_PORT, and PROXY_PORT are required");
}

const UI_PATHS = new Set(["/", "/favicon.ico", "/icon.png", "/site.webmanifest"]);

function isUiPath(pathname) {
  return UI_PATHS.has(pathname) || pathname.startsWith("/_next/") || pathname.startsWith("/brand/");
}

function filteredHeaders(headers) {
  const next = {};
  for (const [key, value] of Object.entries(headers)) {
    if (value == null) continue;
    const lowered = key.toLowerCase();
    if (["transfer-encoding", "connection"].includes(lowered)) continue;
    next[key] = value;
  }
  return next;
}

function targetForPath(pathname) {
  if (isUiPath(pathname)) return { host: UI_HOST, port: UI_PORT };
  return { host: API_HOST, port: API_PORT };
}

const server = http.createServer((req, res) => {
  const pathname = (req.url || "/").split("?")[0] || "/";
  const target = targetForPath(pathname);
  const headers = { ...req.headers };
  delete headers.host;
  const upstream = http.request(
    {
      hostname: target.host,
      port: target.port,
      path: req.url || "/",
      method: req.method,
      headers,
    },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, filteredHeaders(upstreamRes.headers));
      upstreamRes.pipe(res);
    },
  );
  upstream.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end(`Proxy error: $${error.message}`);
  });
  req.pipe(upstream);
});

server.on("upgrade", (req, socket, head) => {
  const pathname = (req.url || "/").split("?")[0] || "/";
  const target = targetForPath(pathname);
  const upstream = net.connect(target.port, target.host, () => {
    const headerLines = [];
    headerLines.push(`GET $${req.url || "/"} HTTP/$${req.httpVersion}`);
    for (const [key, value] of Object.entries(req.headers)) {
      if (value == null) continue;
      if (key.toLowerCase() === "host") {
        headerLines.push(`Host: $${target.host}:$${target.port}`);
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

    pkill -f "litellm --config litellm_config.yaml --port $KDENSE_LITELLM_PORT" >/dev/null 2>&1 || true
    pkill -f "uvicorn server:app --host 127.0.0.1 --port $KDENSE_API_PORT" >/dev/null 2>&1 || true
    pkill -f "next dev --hostname 127.0.0.1 --port $KDENSE_UI_PORT" >/dev/null 2>&1 || true
    pkill -f "next start --hostname 127.0.0.1 --port $KDENSE_UI_PORT" >/dev/null 2>&1 || true
    pkill -f "node /tmp/coder-mounted-proxy.mjs" >/dev/null 2>&1 || true

    nohup sh -lc "cd '$KDENSE_SRC_DIR' && export PATH='/home/coder/.local/bin:/home/coder/.cargo/bin:$PATH' && uv run litellm --config litellm_config.yaml --port $KDENSE_LITELLM_PORT" >/tmp/kdense-litellm.log 2>&1 &
    nohup sh -lc "cd '$KDENSE_SRC_DIR' && export PATH='/home/coder/.local/bin:/home/coder/.cargo/bin:$PATH' && uv run uvicorn server:app --host 127.0.0.1 --port $KDENSE_API_PORT" >/tmp/kdense-backend.log 2>&1 &
    nohup sh -lc "cd '$KDENSE_SRC_DIR/web' && NEXT_PUBLIC_ADK_API_URL= npm run start -- --hostname 127.0.0.1 --port $KDENSE_UI_PORT" >/tmp/kdense-frontend.log 2>&1 &
    nohup env UI_PORT="$KDENSE_UI_PORT" API_PORT="$KDENSE_API_PORT" PROXY_PORT="$KDENSE_PROXY_PORT" node /tmp/coder-mounted-proxy.mjs >/tmp/kdense-proxy.log 2>&1 &
    if [ "$KDENSE_NEEDS_PREP" = true ]; then
      nohup sh -lc "cd '$KDENSE_SRC_DIR' && export PATH='/home/coder/.local/bin:/home/coder/.cargo/bin:$PATH' && uv run python prep_sandbox.py" >/tmp/kdense-prep.log 2>&1 &
    fi

    wait_for_http "http://127.0.0.1:$KDENSE_API_PORT/health" 180
    wait_for_http "http://127.0.0.1:$KDENSE_PROXY_PORT/health" 180
BOOT
    chmod +x /tmp/kdense-bootstrap.sh
    nohup bash /tmp/kdense-bootstrap.sh >/tmp/kdense-bootstrap.log 2>&1 &
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

resource "coder_app" "kdense_byok" {
  agent_id     = coder_agent.main.id
  slug         = "kdense-byok"
  display_name = "K-Dense BYOK"
  icon         = "https://raw.githubusercontent.com/K-Dense-AI/k-dense-byok/main/web/public/brand/kdense-logo-dark.png"
  url          = "http://localhost:3001"
  share        = "owner"
  subdomain    = true
  order        = 2

  healthcheck {
    url       = "http://localhost:3001/health"
    interval  = 5
    threshold = 24
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
    "KDENSE_PROVIDER=${data.coder_parameter.kdense_provider.value}",
    "KDENSE_DEFAULT_MODEL=${data.coder_parameter.kdense_default_model.value}",
    "KDENSE_EXPERT_MODEL=${data.coder_parameter.kdense_expert_model.value}",
    "KDENSE_SEARCH_PROVIDER=${data.coder_parameter.kdense_search_provider.value}",
    "KDENSE_OPENROUTER_API_KEY=${data.coder_parameter.kdense_openrouter_api_key.value}",
    "KDENSE_OPENROUTER_BASE_URL=${data.coder_parameter.kdense_openrouter_base_url.value}",
    "KDENSE_OPENCODE_GO_API_KEY=${data.coder_parameter.kdense_opencode_go_api_key.value}",
    "KDENSE_OPENCODE_GO_BASE_URL=${data.coder_parameter.kdense_opencode_go_base_url.value}",
    "KDENSE_EXA_API_KEY=${data.coder_parameter.kdense_exa_api_key.value}",
    "KDENSE_PARALLEL_API_KEY=${data.coder_parameter.kdense_parallel_api_key.value}",
    "KDENSE_MODAL_TOKEN_ID=${data.coder_parameter.kdense_modal_token_id.value}",
    "KDENSE_MODAL_TOKEN_SECRET=${data.coder_parameter.kdense_modal_token_secret.value}",
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
