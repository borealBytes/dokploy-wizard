from __future__ import annotations

import json
from dataclasses import dataclass

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from tests.integration.coder_migration_remote_protocol import RemoteCoderResponse


@dataclass(frozen=True, slots=True)
class WorkspaceBuildDetail:
    http_category: str
    job_status_category: str
    error_code_category: str
    error_code_present: bool
    job_error_present: bool
    worker_assigned: bool
    resource_category: str
    agent_state_category: str


def parse_workspace_build_detail(response: RemoteCoderResponse) -> WorkspaceBuildDetail:
    """Project a workspace-build response to safe, finite diagnostic categories."""
    if response.status != 200:
        return WorkspaceBuildDetail(
            http_category="non-ok",
            job_status_category="absent",
            error_code_category="absent",
            error_code_present=False,
            job_error_present=False,
            worker_assigned=False,
            resource_category="absent",
            agent_state_category="absent",
        )
    try:
        value: JsonValue = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return WorkspaceBuildDetail(
            http_category="invalid-json",
            job_status_category="absent",
            error_code_category="absent",
            error_code_present=False,
            job_error_present=False,
            worker_assigned=False,
            resource_category="absent",
            agent_state_category="absent",
        )
    match value:
        case dict() as detail:
            job = _mapping(detail.get("job"))
            resources = detail.get("resources")
            return WorkspaceBuildDetail(
                http_category="ok",
                job_status_category=_job_status_category(job),
                error_code_category=_error_code_category(job),
                error_code_present=_text_present(job, "error_code"),
                job_error_present=_text_present(job, "error"),
                worker_assigned=_text_present(job, "worker_id"),
                resource_category=_resource_category(resources),
                agent_state_category=_agent_state_category(resources),
            )
        case _:
            return WorkspaceBuildDetail(
                http_category="invalid-shape",
                job_status_category="absent",
                error_code_category="absent",
                error_code_present=False,
                job_error_present=False,
                worker_assigned=False,
                resource_category="absent",
                agent_state_category="absent",
            )


def _mapping(value: JsonValue | None) -> dict[str, JsonValue] | None:
    match value:
        case dict() as mapping:
            return mapping
        case _:
            return None


def _text_present(mapping: dict[str, JsonValue] | None, key: str) -> bool:
    if mapping is None:
        return False
    match mapping.get(key):
        case str() as text:
            return bool(text)
        case _:
            return False


def _job_status_category(job: dict[str, JsonValue] | None) -> str:
    if job is None:
        return "absent"
    match job.get("status"):
        case "succeeded":
            return "succeeded"
        case "failed":
            return "failed"
        case "running":
            return "running"
        case "pending":
            return "pending"
        case "canceled":
            return "canceled"
        case str():
            return "other"
        case _:
            return "absent"


def _error_code_category(job: dict[str, JsonValue] | None) -> str:
    if job is None:
        return "absent"
    match job.get("error_code"):
        case str() as code if "agent" in code.lower():
            return "agent"
        case str() as code if "provisioner" in code.lower():
            return "provisioner"
        case str() as code if "terraform" in code.lower():
            return "terraform"
        case str() as code if "docker" in code.lower():
            return "docker"
        case str():
            return "other"
        case _:
            return "absent"


def _resource_category(resources: JsonValue | None) -> str:
    match resources:
        case list() if resources:
            return "present"
        case list():
            return "empty"
        case _:
            return "absent"


def _agent_state_category(resources: JsonValue | None) -> str:
    match resources:
        case list() as entries:
            for entry in entries:
                match _mapping(entry):
                    case {"agents": list() as agents}:
                        for agent in agents:
                            match _mapping(agent):
                                case {"status": "connected"}:
                                    return "connected"
                                case {"status": "connecting"}:
                                    return "connecting"
                                case {"status": "disconnected"}:
                                    return "disconnected"
                                case {"status": str()}:
                                    return "other"
                                case _:
                                    continue
                    case _:
                        continue
            return "absent"
        case _:
            return "absent"
