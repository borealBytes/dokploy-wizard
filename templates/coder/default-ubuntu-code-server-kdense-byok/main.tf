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
  username      = data.coder_workspace_owner.me.name
  runtime_image = data.coder_provisioner.me.arch == "amd64" ? "__DOKPLOY_WIZARD_RUNTIME_IMAGE_AMD64__" : "__DOKPLOY_WIZARD_RUNTIME_IMAGE_ARM64__"
  kdense_model_sync_command = <<-EOT
    set -eu
    export KDENSE_WIZARD_CENTRAL_ONLY="1"
    export KDENSE_LITELLM_BASE_URL="__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__"
    export KDENSE_LITELLM_API_KEY="__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__"
    export DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS="__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__/__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__"
    if [ "$${KDENSE_REFRESH_WITH_SUPERVISOR:-0}" = "1" ]; then
      exec python3 /opt/dokploy-wizard/model-sync/workspace-catalog-sync.pyz --adapter kdense --workspace-root /home/coder --kdense-pid-file /home/coder/.local/state/kdense-supervisor.pid --kdense-supervisor /home/coder/.local/bin/kdense-supervisor
    fi
    python3 /opt/dokploy-wizard/model-sync/workspace-catalog-sync.pyz --adapter kdense --workspace-root /home/coder
  EOT
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"
  dir  = "/home/coder"

  startup_script = <<-EOT
    set -eu

    for runtime_command in python3 node; do
      command -v "$runtime_command" >/dev/null 2>&1
    done

    runtime_source=/opt/dokploy-wizard/runtime/install/kdense/source
    runtime_skills=/opt/dokploy-wizard/runtime/install/kdense/skills-source/skills
    work_root=/home/coder/.local/share/kdense
    source_root="$work_root/source"
    current_catalog=/home/coder/.local/state/dokploy-wizard/model-sync/current/models.json
    supervisor=/home/coder/.local/bin/kdense-supervisor
    pid_file=/home/coder/.local/state/kdense-supervisor.pid

    if [ ! -f "$source_root/start.mjs" ]; then
      mkdir -p "$work_root" /home/coder/.local/bin /home/coder/.local/state
      cp -a "$runtime_source" "$source_root"
      rm -f "$source_root/web/src/data/models.json"
      ln -s "$current_catalog" "$source_root/web/src/data/models.json"
      ln -s "$runtime_skills" "$source_root/scientific-skills"
      cat >"$supervisor" <<'SUPERVISOR'
    #!/bin/sh
    set -eu
    pid_file=/home/coder/.local/state/kdense-supervisor.pid
    source_root=/home/coder/.local/share/kdense/source
    node=/opt/dokploy-wizard/runtime/install/kdense/tools/node/bin/node
    trap 'rm -f "$pid_file"' EXIT
    printf '%s\n' "$$" >"$pid_file"
    export KDENSE_WIZARD_CENTRAL_ONLY=1
    export KDENSE_LITELLM_BASE_URL
    export KDENSE_LITELLM_API_KEY
    export KDENSE_DEFAULT_MODEL="$DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS"
    export KDENSE_SCIENTIFIC_SKILLS_DIR="$source_root/scientific-skills"
    cd "$source_root"
    exec "$node" start.mjs
    SUPERVISOR
      chmod 700 "$supervisor"
    fi
  EOT
}

resource "coder_script" "kdense_bootstrap" {
  agent_id           = coder_agent.main.id
  display_name       = "Refresh K-Dense central catalog"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = <<-EOT
    ${local.kdense_model_sync_command}
    pid_file=/home/coder/.local/state/kdense-supervisor.pid
    supervisor=/home/coder/.local/bin/kdense-supervisor
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
      exit 0
    fi
    rm -f "$pid_file"
    nohup "$supervisor" >/tmp/kdense-supervisor.log 2>&1 &
  EOT
}

resource "coder_script" "kdense_model_sync_periodic" {
  agent_id     = coder_agent.main.id
  display_name = "Refresh K-Dense central catalog every 15 minutes"
  cron         = "0 */15 * * * *"
  script       = <<-EOT
    export KDENSE_REFRESH_WITH_SUPERVISOR=1
    ${local.kdense_model_sync_command}
  EOT
}

module "code-server" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/code-server/coder"
  version  = "1.5.2"
  agent_id = coder_agent.main.id
  folder   = "/home/coder"
  order    = 1
}

resource "coder_app" "kdense" {
  agent_id     = coder_agent.main.id
  slug         = "kdense"
  display_name = "K-Dense"
  url          = "http://localhost:3000"
  share        = "owner"
  subdomain    = true
  order        = 2

  healthcheck {
    url       = "http://localhost:3000/health"
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

resource "docker_image" "workspace" {
  name = local.runtime_image
}

resource "docker_container" "workspace" {
  count    = data.coder_workspace.me.start_count
  image    = docker_image.workspace.image_id
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
    "KDENSE_WIZARD_CENTRAL_ONLY=1",
    "KDENSE_LITELLM_BASE_URL=__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__",
    "KDENSE_LITELLM_API_KEY=__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__",
    "DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS=__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__/__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__",
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
