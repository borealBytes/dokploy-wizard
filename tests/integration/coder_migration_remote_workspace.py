from __future__ import annotations

import time
from collections.abc import Callable

from dokploy_wizard.dokploy.coder_migration_api import CoderMigrationApi
from tests.integration.coder_migration_remote_build_detail import parse_workspace_build_detail
from tests.integration.coder_migration_remote_protocol import (
    RemoteCoderProtocolError,
    RemoteCoderResponse,
    workspace_build_failure_category,
)


def wait_for_workspace_status(
    api: CoderMigrationApi,
    read: Callable[[str], RemoteCoderResponse],
    workspace_id: str,
    expected: str,
    probe_runtime: Callable[[], str],
) -> None:
    deadline = time.monotonic() + 120
    last_status = "absent"
    while time.monotonic() < deadline:
        workspace = next(
            (item for item in api.list_workspaces() if str(item.id) == workspace_id), None
        )
        if workspace is not None:
            last_status = workspace.latest_build.status
            if last_status == expected:
                return
            if last_status in {"failed", "canceled", "deleted"}:
                detail_response = read(f"/api/v2/workspacebuilds/{workspace.latest_build.id}")
                category = workspace_build_failure_category(detail_response)
                detail = parse_workspace_build_detail(detail_response)
                runtime = probe_runtime()
                raise RemoteCoderProtocolError(
                    "remote workspace build "
                    f"category={category} http={detail.http_category} "
                    f"job={detail.job_status_category} code={detail.error_code_category} "
                    f"error={detail.job_error_present} worker={detail.worker_assigned} "
                    f"resources={detail.resource_category} "
                    f"agent={detail.agent_state_category} runtime={runtime}"
                )
        time.sleep(0.25)
    raise RemoteCoderProtocolError(f"remote workspace lifecycle is {last_status}")
