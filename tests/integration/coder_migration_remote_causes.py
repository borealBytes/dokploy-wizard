from __future__ import annotations

import traceback
from pathlib import Path

from tests.integration.coder_migration_remote_http import RemoteCoderTransportError
from tests.integration.coder_migration_remote_protocol import RemoteCoderProtocolError


def cause(error: BaseException) -> str:
    match error:
        case RemoteCoderTransportError():
            return "transport"
        case RemoteCoderProtocolError():
            reason = str(error)
            for marker, category in _WORKSPACE_BUILD_MARKERS:
                if marker in reason:
                    return category
            for category in _RUNTIME_CATEGORIES:
                if f"runtime={category}" in reason:
                    return category
            for marker, category in _MARKERS:
                if marker in reason:
                    return category
            if "workspace build category=workspace-build-unknown" in reason:
                return (
                    "agent-connectivity"
                    if "agent=connecting" in reason
                    else "workspace-build-unknown"
                )
            if "template-push" in reason:
                return _template_cause(reason)
            return _protocol_origin(error)
        case ValueError() as value:
            if "Coder error.validations is missing" in str(value):
                return "coder-error-validations-missing"
            if "Coder error.validations is null" in str(value):
                return "coder-error-validations-null"
            return _exception_origin(error, "value")
        case RuntimeError():
            return _exception_origin(error, "runtime")
        case _:
            return _exception_origin(error, "unexpected")


_MARKERS = (
    ("docker-socket-metadata", "docker-socket-metadata"),
    ("docker-socket-type", "docker-socket-type"),
    ("provisioner inventory command", "provisioner-inventory-command"),
    ("provisioner inventory is not JSON", "provisioner-inventory-json"),
    ("provisioner inventory is not an array", "provisioner-inventory-shape"),
    ("provisioner container running state", "provisioner-container-state"),
    ("provisioner inventory did not stabilize", "provisioner-registration"),
    ("workspace lifecycle is starting", "workspace-starting"),
    ("workspace lifecycle is pending", "workspace-pending"),
    ("workspace lifecycle is stopping", "workspace-stopping"),
    ("workspace lifecycle is failed", "workspace-failed"),
    ("workspace lifecycle is canceled", "workspace-canceled"),
    ("workspace lifecycle is deleted", "workspace-deleted"),
    ("workspace build category template-build", "template-build"),
    ("workspace build category provisioner-build", "provisioner-build"),
    ("workspace build category agent-connectivity", "agent-connectivity"),
    ("workspace build category build-deadline", "build-deadline"),
    ("workspace build category workspace-build-detail-http", "workspace-build-detail-http"),
    ("workspace lifecycle", "workspace-lifecycle"),
    ("workspace did not reach", "workspace-lifecycle"),
    ("concurrent Coder start did not block", "concurrent-delete-not-blocked"),
    ("workspace delete hook did not observe a receipt", "concurrent-delete-hook-receipt"),
    ("workspace delete hook did not submit a start build", "concurrent-delete-hook-start"),
    ("workspace delete hook received duplicate", "concurrent-delete-hook-duplicate"),
    ("blocked Coder delete receipt is incomplete", "concurrent-delete-receipt"),
    ("blocked Coder delete step is incomplete", "concurrent-delete-receipt-step"),
    ("blocked Coder delete receipt did not advance", "concurrent-delete-receipt-cas"),
    ("build history does not contain the injected start", "concurrent-delete-start-history"),
    ("build history unexpectedly contains delete", "concurrent-delete-delete-history"),
    ("Coder start build must return HTTP 201", "concurrent-delete-start-response"),
    ("healthz", "healthz"),
    ("buildinfo", "buildinfo"),
    ("creation", "first-user-create"),
    ("login", "login"),
    ("template-copy", "template-copy"),
    ("template-directory", "template-directory"),
    ("first-user", "first-user"),
    ("first user", "first-user"),
    ("deadline", "deadline"),
)

_WORKSPACE_BUILD_MARKERS = (
    ("workspace build category=template-build", "template-build"),
    ("workspace build category=provisioner-build", "provisioner-build"),
    ("workspace build category=agent-connectivity", "agent-connectivity"),
    ("workspace build category=build-deadline", "build-deadline"),
    ("workspace build category=workspace-build-detail-http", "workspace-build-detail-http"),
)

_RUNTIME_CATEGORIES = (
    "workspace-container-missing",
    "workspace-container-exited",
    "workspace-network-detached",
    "workspace-dns-unreachable",
    "workspace-coder-http-unreachable",
    "workspace-agent-process-absent",
    "workspace-agent-registration",
    "workspace-runtime-unknown",
)


def _template_cause(reason: str) -> str:
    if "provisioner" in reason:
        return "template-provisioner"
    if "terraform" in reason:
        return "template-terraform"
    if "authorization" in reason:
        return "template-authorization"
    return "template-push"


def _protocol_origin(error: RemoteCoderProtocolError) -> str:
    entries = traceback.extract_tb(error.__traceback__)
    if not entries:
        return f"protocol-{type(error).__name__.lower()}"
    entry = entries[-1]
    match Path(entry.filename).name, entry.name:
        case "coder_migration_receipt_schema.py", "_timestamp":
            return "protocol-receipt-timestamp"
        case "coder_migration_receipt_schema.py", _:
            return "protocol-receipt-schema"
        case "coder_migration_remote_main.py", "_exercise_concurrent_delete":
            return "concurrent-delete-protocol"
        case "coder_migration_remote_main.py", "_assert_history":
            return "concurrent-delete-history"
        case "coder_migration_remote_seed.py", _:
            return "protocol-seed"
        case "coder_migration_api.py", _:
            return "protocol-api"
        case _:
            stem = Path(entry.filename).stem.replace("_", "-")
            function = entry.name if entry.name.isidentifier() else "module"
            return f"protocol-{stem}-{function}"


def _exception_origin(error: BaseException, category: str) -> str:
    entries = traceback.extract_tb(error.__traceback__)
    if not entries:
        return f"{category}-{type(error).__name__.lower()}"
    entry = entries[-1]
    stem = Path(entry.filename).stem.replace("_", "-")
    function = entry.name if entry.name.isidentifier() else "module"
    return f"{category}-{stem}-{function}"
