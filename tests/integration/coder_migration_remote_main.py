from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from dokploy_wizard.dokploy.coder_migration_operations import (
    CoderMigrationBlockedError,
    CoderMigrationDependencies,
    CoderMigrationOperations,
    WorkspaceDeletionRequest,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import MigrationReceipt
from dokploy_wizard.dokploy.coder_migration_receipts import CoderMigrationReceiptStore
from dokploy_wizard.dokploy.coder_migration_types import CoderBuild, CoderProtocolError
from tests.integration.coder_migration_remote_causes import cause as _cause
from tests.integration.coder_migration_remote_docker import RemoteCoderStack, RuntimeImages
from tests.integration.coder_migration_remote_http import RemoteCoderTransportError
from tests.integration.coder_migration_remote_protocol import RemoteCoderProtocolError
from tests.integration.coder_migration_remote_result import (
    RemoteCoderRuntimeResult,
    result_bytes,
)
from tests.integration.coder_migration_remote_seed import RemoteCoderSeeder, SeededCoderWorkspace

_POSTGRES_IMAGE = (
    "cimg/postgres:16.0@sha256:"
    "b125148bc76e8e8eee5eb3ad6020a3a14110a14e8192f1c645128afebe2e2f84"
)
_TASK1_CODER_IMAGE: Final = (
    "ghcr.io/coder/coder@sha256:"
    "8ccba928849cb6d857d6a59184618a2017626708c513251aac74768fdb477f78"
)


@dataclass(frozen=True, slots=True)
class ConcurrentStartContext:
    seeder: RemoteCoderSeeder
    workspace_id: str
    receipt_store: CoderMigrationReceiptStore


class StartBeforeDelete:
    def __init__(self, context: ConcurrentStartContext) -> None:
        self._context = context
        self._before_start_receipt: MigrationReceipt | None = None
        self._started_build: CoderBuild | None = None

    @property
    def before_start_receipt(self) -> MigrationReceipt:
        if self._before_start_receipt is None:
            raise CoderProtocolError("workspace delete hook did not observe a receipt")
        return self._before_start_receipt

    @property
    def started_build(self) -> CoderBuild:
        if self._started_build is None:
            raise CoderProtocolError("workspace delete hook did not submit a start build")
        return self._started_build

    def hit(self, point: str) -> None:
        if point != "before_request":
            return
        if self._started_build is not None:
            raise CoderProtocolError("workspace delete hook received duplicate before_request")
        receipt = self._context.receipt_store.load()
        if receipt is None:
            raise CoderProtocolError("workspace delete hook requires a write-ahead receipt")
        self._before_start_receipt = receipt
        self._started_build = self._context.seeder.submit_build(self._context.workspace_id, "start")


def run() -> RemoteCoderRuntimeResult:
    result = _failed_result("stack", "protocol")
    stack: RemoteCoderStack | None = None
    seeder: RemoteCoderSeeder | None = None
    phase = "stack"
    try:
        stack = RemoteCoderStack(_images())
        endpoint = stack.start()
        phase = "seed"
        seeder = RemoteCoderSeeder(
            endpoint,
            stack.coder_container,
            stack.wait_for_provisioners,
            stack.template_address,
            stack.register_workspace_container,
            stack.probe_workspace_runtime,
        )
        seeded = seeder.seed_workspace()
        phase = "concurrent-delete"
        result = _exercise_concurrent_delete(seeded)
    except (RemoteCoderProtocolError, RemoteCoderTransportError) as error:
        failed_phase = seeder.phase if seeder is not None and phase == "seed" else phase
        result = _failed_result(failed_phase, _cause(error))
    except OSError:
        result = _failed_result(phase, "stack-os")
    except CoderMigrationBlockedError:
        result = _failed_result(phase, "migration-blocked")
    except (CoderProtocolError, RuntimeError) as error:
        failed_phase = seeder.phase if seeder is not None and phase == "seed" else phase
        result = _failed_result(failed_phase, _cause(error))
    finally:
        cleanup = "complete" if stack is None or stack.cleanup() else "failed"
        result = replace(result, cleanup=cleanup)
        if cleanup != "complete":
            result = replace(result, status="failed", cause="cleanup")
    return result


def _exercise_concurrent_delete(seeded: SeededCoderWorkspace) -> RemoteCoderRuntimeResult:
    with TemporaryDirectory(prefix="task4-coder-receipt-") as directory:
        receipt_store = CoderMigrationReceiptStore(Path(directory))
        hook = StartBeforeDelete(
            ConcurrentStartContext(
                seeder=seeded.seeder,
                workspace_id=seeded.workspace_id,
                receipt_store=receipt_store,
            )
        )
        operations = CoderMigrationOperations(
            CoderMigrationDependencies(
                api=seeded.api,
                receipt_store=receipt_store,
                clock=lambda: "2026-07-27T00:00:00Z",
                crash_hook=hook,
            )
        )
        try:
            operations.delete_workspace(
                WorkspaceDeletionRequest(
                    operation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    desired_fingerprint="a" * 64,
                    workspace_id=seeded.workspace_id,
                )
            )
        except CoderMigrationBlockedError:
            pass
        else:
            raise CoderProtocolError("concurrent Coder start did not block workspace delete")
        receipt = receipt_store.load()
        if receipt is None or receipt.status != "blocked" or len(receipt.steps) != 1:
            raise CoderProtocolError("blocked Coder delete receipt is incomplete")
        if receipt.steps[0].status != "blocked":
            raise CoderProtocolError("blocked Coder delete step is incomplete")
        before_start = hook.before_start_receipt
        token_advanced = receipt.cas_token != before_start.cas_token
        if receipt.generation <= before_start.generation or not token_advanced:
            raise CoderProtocolError(
                "blocked Coder delete receipt did not advance its CAS generation"
            )
        _assert_history(seeded, hook.started_build)
        return RemoteCoderRuntimeResult(
            status="passed",
            phase="assertions",
            cause="none",
            skipped=0,
            start_transition=True,
            delete_transition=False,
            receipt_status=receipt.status,
            receipt_generation=receipt.generation,
            receipt_token_advanced=token_advanced,
            cleanup="pending",
        )


def _assert_history(seeded: SeededCoderWorkspace, started_build: CoderBuild) -> None:
    builds = seeded.api.list_workspace_builds(seeded.workspace_id)
    if not any(build.id == started_build.id and build.transition == "start" for build in builds):
        raise CoderProtocolError("remote Coder build history does not contain the injected start")
    if any(build.transition == "delete" for build in builds):
        raise CoderProtocolError("remote Coder build history unexpectedly contains delete")


def _images() -> RuntimeImages:
    try:
        coder = os.environ["CODER_TEST_IMAGE"]
    except KeyError as error:
        raise CoderProtocolError("CODER_TEST_IMAGE is required") from error
    if coder != _TASK1_CODER_IMAGE:
        raise CoderProtocolError(
            "CODER_TEST_IMAGE must equal the Task 1 full Coder image reference"
        )
    return RuntimeImages(coder=coder, postgres=_POSTGRES_IMAGE)


def _failed_result(phase: str, cause: str) -> RemoteCoderRuntimeResult:
    return RemoteCoderRuntimeResult(
        status="failed",
        phase=phase,
        cause=cause,
        skipped=0,
        start_transition=False,
        delete_transition=False,
        receipt_status="unavailable",
        receipt_generation=0,
        receipt_token_advanced=False,
        cleanup="pending",
    )


def _emit(result: RemoteCoderRuntimeResult) -> int:
    print(result_bytes(result).decode("utf-8"))
    return 0


def main() -> int:
    return _emit(run())


if __name__ == "__main__":
    raise SystemExit(main())
