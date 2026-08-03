"""Strict parsing and invariant checks for persisted workspace transactions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    JsonValue,
    PreState,
    ProcessIdentity,
    ProcessStatus,
    RuntimeIdentityError,
    TargetKind,
    TargetReceipt,
    TargetStatus,
    TransactionPhase,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_transition import (
    validate_record_invariants,
)


def parse_transaction_record(value: Mapping[str, JsonValue]) -> TransactionRecord:
    """Parse one exact-version transaction JSON object into typed state."""
    required = {
        "schema_version", "generation", "cas_token", "phase", "created_at", "updated_at",
        "catalog_sha256", "targets", "processes", "error",
    }
    if set(value) != required:
        raise WorkspaceCatalogSyncError("workspace transaction schema is invalid")
    schema_version = positive(value["schema_version"], "schema_version")
    if schema_version not in {1, 2}:
        raise WorkspaceCatalogSyncError("workspace transaction schema is invalid")
    record = TransactionRecord(
        generation=positive(value["generation"], "generation"),
        cas_token=sha(value["cas_token"], "cas_token"),
        phase=parse_phase(value["phase"]),
        created_at=timestamp(value["created_at"], "created_at"),
        updated_at=timestamp(value["updated_at"], "updated_at"),
        catalog_sha256=sha(value["catalog_sha256"], "catalog_sha256"),
        targets=tuple(
            parse_target(item, schema_version=schema_version)
            for item in sequence(value["targets"], "targets")
        ),
        processes=tuple(parse_process(item) for item in sequence(value["processes"], "processes")),
        error=optional_text(value["error"], "error"),
    )
    validate_transaction_record(record)
    return record


def validate_transaction_record(record: TransactionRecord) -> None:
    """Validate typed cross-field invariants before durable publication."""
    if not record.targets:
        raise WorkspaceCatalogSyncError("workspace transaction targets are empty")
    if record.updated_at < record.created_at:
        raise WorkspaceCatalogSyncError("workspace transaction timestamps are invalid")
    if len({target.path for target in record.targets}) != len(record.targets):
        raise WorkspaceCatalogSyncError("workspace transaction target paths are not unique")
    if len({process.name for process in record.processes}) != len(record.processes):
        raise RuntimeIdentityError("workspace process names are not unique")
    if any(process.generation != record.generation for process in record.processes):
        raise RuntimeIdentityError("workspace process generation does not match transaction")
    validate_record_invariants(record)


def parse_target(value: JsonValue, *, schema_version: int) -> TargetReceipt:
    """Parse one target receipt with all preimage fields present."""
    mapping = require_mapping(value, "target")
    required = {
        "path", "kind", "pre_state", "pre_sha256", "pre_mode", "pre_target",
        "staged_sha256", "post_sha256", "status",
    }
    if schema_version == 2:
        required.add("post_mode")
    if set(mapping) != required:
        raise WorkspaceCatalogSyncError("workspace target schema is invalid")
    kind = parse_target_kind(mapping["kind"])
    pre_state = parse_pre_state(mapping["pre_state"])
    pre_mode = optional_mode(mapping["pre_mode"], "target pre_mode")
    if schema_version == 1:
        if kind == "file" and pre_state != "file":
            raise WorkspaceCatalogSyncError("workspace v1 target mode is unrecoverable")
        post_mode = pre_mode if kind == "file" else None
    else:
        post_mode = optional_mode(mapping["post_mode"], "target post_mode")
    return TargetReceipt(
        path=text(mapping["path"], "target path"), kind=kind,
        pre_state=pre_state,
        pre_sha256=optional_sha(mapping["pre_sha256"], "target pre_sha256"),
        pre_mode=pre_mode,
        pre_target=optional_text(mapping["pre_target"], "target pre_target"),
        post_mode=post_mode,
        staged_sha256=sha(mapping["staged_sha256"], "target staged_sha256"),
        post_sha256=optional_sha(mapping["post_sha256"], "target post_sha256"),
        status=parse_target_status(mapping["status"]),
    )


def parse_process(value: JsonValue) -> ProcessIdentity:
    """Parse one strict process identity."""
    mapping = require_mapping(value, "process")
    required = {
        "name", "pid", "start_time_ticks", "argv_sha256", "executable_sha256", "generation",
        "status",
    }
    if set(mapping) != required:
        raise RuntimeIdentityError("workspace process schema is invalid")
    return ProcessIdentity(
        name=text(mapping["name"], "process name"), pid=positive(mapping["pid"], "process pid"),
        start_time_ticks=positive(mapping["start_time_ticks"], "process start_time_ticks"),
        argv_sha256=sha(mapping["argv_sha256"], "process argv_sha256"),
        executable_sha256=sha(mapping["executable_sha256"], "process executable_sha256"),
        generation=positive(mapping["generation"], "process generation"),
        status=parse_process_status(mapping["status"]),
    )


def require_mapping(value: JsonValue, label: str) -> Mapping[str, JsonValue]:
    """Require one JSON object."""
    if not isinstance(value, dict):
        raise WorkspaceCatalogSyncError(f"{label} must be an object")
    return value


def sequence(value: JsonValue, label: str) -> list[JsonValue]:
    """Require one JSON array."""
    if not isinstance(value, list):
        raise WorkspaceCatalogSyncError(f"{label} must be a list")
    return value


def text(value: JsonValue, label: str) -> str:
    """Require nonempty JSON string text."""
    if not isinstance(value, str) or not value:
        raise WorkspaceCatalogSyncError(f"{label} is invalid")
    return value


def optional_text(value: JsonValue, label: str) -> str | None:
    """Parse optional nonempty text."""
    return None if value is None else text(value, label)


def positive(value: JsonValue, label: str) -> int:
    """Require a non-boolean positive integer identifier."""
    if type(value) is not int or value < 1:
        raise WorkspaceCatalogSyncError(f"{label} is invalid")
    return value


def sha(value: JsonValue, label: str) -> str:
    """Require lowercase 64-hex digest text."""
    result = text(value, label)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise WorkspaceCatalogSyncError(f"{label} is invalid")
    return result


def optional_sha(value: JsonValue, label: str) -> str | None:
    """Parse an optional SHA-256 digest."""
    return None if value is None else sha(value, label)


def optional_mode(value: JsonValue, label: str) -> str | None:
    """Parse an optional four-digit permission mode."""
    if value is None:
        return None
    result = text(value, label)
    if len(result) != 4 or any(character not in "01234567" for character in result):
        raise WorkspaceCatalogSyncError(f"{label} is invalid")
    return result


def timestamp(value: JsonValue, label: str) -> str:
    """Require one canonical UTC-second timestamp."""
    result = text(value, label)
    try:
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise WorkspaceCatalogSyncError(f"{label} is invalid") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != result:
        raise WorkspaceCatalogSyncError(f"{label} is invalid")
    return result


def parse_phase(value: JsonValue) -> TransactionPhase:
    """Parse one legal transaction phase."""
    match text(value, "phase"):
        case "prepared":
            return "prepared"
        case "files_written":
            return "files_written"
        case "pre_switch_verified":
            return "pre_switch_verified"
        case "switched":
            return "switched"
        case "processes_stopped":
            return "processes_stopped"
        case "processes_started":
            return "processes_started"
        case "health_verified":
            return "health_verified"
        case "committed":
            return "committed"
        case "rollback_started":
            return "rollback_started"
        case "rolled_back":
            return "rolled_back"
        case "blocked":
            return "blocked"
        case _:
            raise WorkspaceCatalogSyncError("workspace transaction phase is invalid")


def parse_target_kind(value: JsonValue) -> TargetKind:
    """Parse one owned target kind."""
    match text(value, "target kind"):
        case "file":
            return "file"
        case "symlink":
            return "symlink"
        case _:
            raise WorkspaceCatalogSyncError("workspace target kind is invalid")


def parse_pre_state(value: JsonValue) -> PreState:
    """Parse one target preimage state."""
    match text(value, "target pre_state"):
        case "absent":
            return "absent"
        case "file":
            return "file"
        case "symlink":
            return "symlink"
        case _:
            raise WorkspaceCatalogSyncError("workspace target state is invalid")


def parse_target_status(value: JsonValue) -> TargetStatus:
    """Parse one legal target status."""
    match text(value, "target status"):
        case "observed":
            return "observed"
        case "prepared":
            return "prepared"
        case "write_intent":
            return "write_intent"
        case "written":
            return "written"
        case "rollback_intent":
            return "rollback_intent"
        case "rolled_back":
            return "rolled_back"
        case _:
            raise WorkspaceCatalogSyncError("workspace target status is invalid")


def parse_process_status(value: JsonValue) -> ProcessStatus:
    """Parse one legal process status."""
    match text(value, "process status"):
        case "observed":
            return "observed"
        case "stop_intent":
            return "stop_intent"
        case "stopped":
            return "stopped"
        case "start_intent":
            return "start_intent"
        case "started":
            return "started"
        case "verified":
            return "verified"
        case _:
            raise RuntimeIdentityError("workspace process status is invalid")
