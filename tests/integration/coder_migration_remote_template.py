from __future__ import annotations

import re
from dataclasses import dataclass

from tests.integration.coder_migration_remote_protocol import RemoteCoderProtocolError

_DOCKER_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}\Z")


@dataclass(frozen=True, slots=True)
class WorkspaceTemplateAddress:
    network_name: str
    workspace_prefix: str

    def __post_init__(self) -> None:
        for value in (self.network_name, self.workspace_prefix):
            if _DOCKER_NAME.fullmatch(value) is None:
                raise RemoteCoderProtocolError("remote template Docker name is invalid")

_TEMPLATE = (
    "terraform {\n"
    "  required_providers {\n"
    "    coder = {\n"
    "      source = \"coder/coder\"\n"
    "    }\n"
    "    docker = {\n"
    "      source = \"kreuzwerker/docker\"\n"
    "    }\n"
    "  }\n"
    "}\n"
    "provider \"coder\" {}\n"
    "provider \"docker\" {}\n"
    "data \"coder_provisioner\" \"me\" {}\n"
    "data \"coder_workspace\" \"me\" {}\n"
    "resource \"coder_agent\" \"main\" {\n"
    "  os = \"linux\"\n"
    "  arch = \"amd64\"\n"
    "}\n"
    "resource \"docker_image\" \"workspace\" {\n"
    "  name = \"codercom/enterprise-base:ubuntu\"\n"
    "}\n"
    "resource \"docker_container\" \"workspace\" {\n"
    "  count = data.coder_workspace.me.start_count\n"
    "  image = docker_image.workspace.image_id\n"
    "  name = \"__WORKSPACE_PREFIX__-${data.coder_workspace.me.id}\"\n"
    "  entrypoint = [\n"
    "    \"sh\",\n"
    "    \"-c\",\n"
    "    coder_agent.main.init_script\n"
    "  ]\n"
    "  env = [\"CODER_AGENT_TOKEN=${coder_agent.main.token}\"]\n"
    "  networks_advanced {\n"
    "    name = \"__NETWORK_NAME__\"\n"
    "  }\n"
    "}\n"
)


def render_template(address: WorkspaceTemplateAddress) -> str:
    return _TEMPLATE.replace("__NETWORK_NAME__", address.network_name).replace(
        "__WORKSPACE_PREFIX__", address.workspace_prefix
    )


TEMPLATE = render_template(WorkspaceTemplateAddress("task4-network", "task4-workspace"))


def template_push_command(container: str, name: str, token: str) -> tuple[str, ...]:
    return (
        "docker",
        "exec",
        "--env",
        "CODER_URL=http://localhost:3000",
        "--env",
        f"CODER_SESSION_TOKEN={token}",
        container,
        "/opt/coder",
        "templates",
        "push",
        name,
        "--directory",
        "/tmp/template",
        "--ignore-lockfile",
        "--yes",
    )
