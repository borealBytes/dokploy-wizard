from __future__ import annotations

import pytest

from dokploy_wizard.dokploy.coder_migration_types import CoderProtocolError
from tests.integration import coder_migration_remote_main
from tests.integration.coder_migration_remote_causes import cause
from tests.integration.coder_migration_remote_diagnostic_result import parse_diagnostic_result
from tests.integration.coder_migration_remote_http import RemoteCoderTransportError
from tests.integration.coder_migration_remote_protocol import RemoteCoderProtocolError
from tests.integration.coder_migration_remote_result import (
    RemoteCoderResultError,
    RemoteCoderRuntimeResult,
    parse_runtime_result,
)
from tests.integration.coder_migration_remote_template import WorkspaceTemplateAddress


def test_runtime_result_rejects_success_without_completed_cleanup() -> None:
    # Given
    payload = (
        b'{"cause":"none","cleanup":"pending","delete_transition":false,'
        b'"phase":"assertions","receipt_generation":1,"receipt_status":"blocked",'
        b'"receipt_token_advanced":true,"schema_version":1,"skipped":0,'
        b'"start_transition":true,"status":"passed"}'
    )

    # When / Then
    with pytest.raises(RemoteCoderResultError, match="cleanup"):
        parse_runtime_result(payload)


def test_runtime_stack_cleanup_runs_when_seed_raises_an_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    class Stack:
        cleanup_calls = 0
        coder_container = "coder"
        template_address = WorkspaceTemplateAddress("task4-network", "task4-workspace")

        def start(self) -> object:
            return object()

        def wait_for_provisioners(self, _: str) -> None:
            return None

        def register_workspace_container(self, _: str) -> None:
            return None

        def probe_workspace_runtime(self) -> str:
            return "workspace-agent-registration"

        def cleanup(self) -> bool:
            self.cleanup_calls += 1
            return True

    class Seeder:
        phase = "seed"

        def __init__(self, *_: object) -> None:
            return None

        def seed_workspace(self) -> object:
            raise SystemExit(23)

    stack = Stack()
    monkeypatch.setenv(
        "CODER_TEST_IMAGE",
        "ghcr.io/coder/coder@sha256:"
        "8ccba928849cb6d857d6a59184618a2017626708c513251aac74768fdb477f78",
    )
    monkeypatch.setattr(
        coder_migration_remote_main,
        "RemoteCoderStack",
        lambda _: stack,
    )
    monkeypatch.setattr(coder_migration_remote_main, "RemoteCoderSeeder", Seeder)

    # When / Then
    with pytest.raises(SystemExit):
        coder_migration_remote_main.run()
    assert stack.cleanup_calls == 1


def test_runtime_rejects_a_different_digest_pinned_coder_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("CODER_TEST_IMAGE", "ghcr.io/coder/coder@sha256:" + "0" * 64)

    # When / Then
    with pytest.raises(CoderProtocolError, match="Task 1"):
        coder_migration_remote_main._images()


def test_runtime_classifies_bootstrap_transport_without_retaining_transport_text() -> None:
    # Given
    error = RemoteCoderTransportError("transport detail")

    # When
    actual_cause = cause(error)

    # Then
    assert actual_cause == "transport"


def test_runtime_classifies_provisioner_inventory_command_without_retaining_stderr() -> None:
    # Given
    error = RemoteCoderProtocolError("remote Coder provisioner inventory command failed")

    # When
    actual_cause = cause(error)

    # Then
    assert actual_cause == "provisioner-inventory-command"


def test_runtime_classifies_workspace_lifecycle_without_retaining_status_detail() -> None:
    # Given
    error = RemoteCoderProtocolError("remote workspace did not reach the expected lifecycle state")

    # When
    actual_cause = cause(error)

    # Then
    assert actual_cause == "workspace-lifecycle"


def test_runtime_classifies_workspace_starting_as_a_closed_lifecycle_state() -> None:
    # Given
    error = RemoteCoderProtocolError("remote workspace lifecycle is starting")

    # When
    actual_cause = cause(error)

    # Then
    assert actual_cause == "workspace-starting"


def test_runtime_classifies_failed_workspace_as_a_closed_lifecycle_state() -> None:
    # Given
    error = RemoteCoderProtocolError("remote workspace lifecycle is failed")

    # When
    actual_cause = cause(error)

    # Then
    assert actual_cause == "workspace-failed"


def test_runtime_prefers_the_workspace_build_category_over_absent_runtime() -> None:
    # Given
    error = RemoteCoderProtocolError(
        "remote workspace build category=template-build runtime=workspace-container-missing"
    )

    # When
    actual_cause = cause(error)

    # Then
    assert actual_cause == "template-build"


def test_runtime_classifies_concurrent_delete_receipt_cas_failure() -> None:
    # Given
    error = RemoteCoderProtocolError(
        "blocked Coder delete receipt did not advance its CAS generation"
    )

    # When
    actual_cause = cause(error)

    # Then
    assert actual_cause == "concurrent-delete-receipt-cas"


def test_runtime_entrypoint_serializes_a_supplied_result() -> None:
    # Given
    result = RemoteCoderRuntimeResult(
        status="failed",
        phase="template",
        cause="template-provisioner",
        skipped=0,
        start_transition=False,
        delete_transition=False,
        receipt_status="unavailable",
        receipt_generation=0,
        receipt_token_advanced=False,
        cleanup="complete",
    )

    # When
    exit_code = coder_migration_remote_main._emit(result)

    # Then
    assert exit_code == 0


def test_remote_diagnostic_parses_the_complete_runtime_stage_report() -> None:
    # Given
    payload = (
        b'{"completed":["package-import","module-import","images",'
        b'"stack-construction","run","serialization","entrypoint"],'
        b'"error_category":"none","error_origin":"none","error_type":"none",'
        b'"runtime_cause":"none",'
        b'"runtime_phase":"assertions","runtime_status":"passed",'
        b'"stage":"entrypoint"}'
    )

    # When
    result = parse_diagnostic_result(payload)

    # Then
    assert result.runtime_status == "passed"
