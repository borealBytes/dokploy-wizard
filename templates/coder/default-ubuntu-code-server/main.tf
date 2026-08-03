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
  model_sync_script = <<-EOT
    set -eu
    export DOKPLOY_WIZARD_LITELLM_DEFAULT_ALIAS="__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__/__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__"
    export DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON="__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__"
    exec python3 /opt/dokploy-wizard/model-sync/workspace-catalog-sync.pyz --adapter primary --workspace-root /home/coder
  EOT
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

    for runtime_command in curl git wget btop python3 opencode zellij node pi; do
      command -v "$runtime_command" >/dev/null 2>&1
    done
  EOT
}

resource "coder_script" "model_sync_start" {
  agent_id           = coder_agent.main.id
  display_name       = "Refresh workspace models"
  run_on_start       = true
  start_blocks_login = false
  script             = local.model_sync_script
}

resource "coder_script" "model_sync_periodic" {
  agent_id     = coder_agent.main.id
  display_name = "Refresh workspace models every 15 minutes"
  cron         = "0 */15 * * * *"
  script       = local.model_sync_script
}

module "code-server" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/code-server/coder"
  version  = "1.5.2"
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

resource "docker_image" "workspace" {
  name = local.runtime_image
}

resource "docker_container" "workspace" {
  count    = data.coder_workspace.me.start_count
  image = docker_image.workspace.image_id
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
