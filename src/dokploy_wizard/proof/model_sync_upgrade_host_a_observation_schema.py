"""Closed-schema parser for Host A remote observation output."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_upgrade_host_a_observation_types import (
    CatalogObservation,
    HostAObservation,
    MigrationObservation,
    MigrationStatus,
    ReleaseObservation,
    ScheduleObservation,
    SyncResultObservation,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_MIGRATION_STATUSES: Final[dict[str, MigrationStatus]] = {
    "blocked": "blocked",
    "completed": "completed",
    "failed": "failed",
    "planned": "planned",
    "running": "running",
}


def parse_host_a_observation(
    payload: Mapping[str, proof.JsonValue], *, remote_root: Path
) -> HostAObservation:
    """Parse one remote observation and bind every authoritative artifact path."""

    if not remote_root.is_absolute():
        raise UpgradeHostAError("Host A observation remote root is invalid")
    data = dict(payload)
    _keys(
        data,
        {"catalog", "migration", "release", "schedule", "schema_version", "sync_results"},
        "observation",
    )
    if data["schema_version"] != 1:
        raise UpgradeHostAError("Host A observation schema is invalid")
    return HostAObservation(
        release=_release(_mapping(data["release"], "release"), remote_root),
        migration=_optional_migration(data["migration"], remote_root),
        catalog=_optional_catalog(data["catalog"]),
        schedule=_optional_schedule(data["schedule"], remote_root),
        sync_results=_sync_results(data["sync_results"]),
    )


def _release(data: dict[str, proof.JsonValue], root: Path) -> ReleaseObservation:
    _keys(
        data,
        {
            "active_release_path",
            "archive_sha256",
            "commit_sha",
            "manifest_mode",
            "manifest_path",
            "manifest_sha256",
        },
        "release",
    )
    archive_sha256 = _sha(data["archive_sha256"], "release archive")
    manifest_path = _path(data["manifest_path"], "release manifest")
    active_path = _path(data["active_release_path"], "active release")
    if (
        manifest_path != root / "release-manifest.json"
        or _mode(data["manifest_mode"], "release manifest") != 0o644
        or active_path != root / "releases" / archive_sha256
    ):
        raise UpgradeHostAError("Host A release manifest path or mode is invalid")
    commit = _text(data["commit_sha"], "release commit")
    if _COMMIT.fullmatch(commit) is None:
        raise UpgradeHostAError("Host A release commit is invalid")
    return ReleaseObservation(
        commit,
        archive_sha256,
        _sha(data["manifest_sha256"], "release manifest"),
        active_path,
    )


def _optional_migration(value: proof.JsonValue, root: Path) -> MigrationObservation | None:
    if value is None:
        return None
    data = _mapping(value, "migration")
    _keys(
        data,
        {
            "mode",
            "mutation_events",
            "path",
            "primary_name",
            "push_names",
            "receipt_sha256",
            "retired_names",
            "status",
        },
        "migration",
    )
    if (
        _path(data["path"], "migration receipt")
        != root / "state" / "coder-template-migration" / "coder-migration-receipts-v1.json"
        or _mode(data["mode"], "migration receipt") != 0o600
    ):
        raise UpgradeHostAError("Host A migration receipt path or mode is invalid")
    return MigrationObservation(
        _migration_status(data["status"]),
        _sha(data["receipt_sha256"], "migration receipt"),
        _sha_sequence(data["mutation_events"], "migration events"),
        _text(data["primary_name"], "migration primary name"),
        _text_sequence(data["push_names"], "migration push names"),
        _text_sequence(data["retired_names"], "migration retired names"),
    )


def _optional_catalog(value: proof.JsonValue) -> CatalogObservation | None:
    if value is None:
        return None
    data = _mapping(value, "catalog")
    _keys(
        data,
        {
            "applied_model_set_sha256",
            "generation_mode",
            "generation_path",
            "generation_sha256",
            "state_mode",
            "state_output_sha256",
            "state_path",
        },
        "catalog",
    )
    if (
        _mode(data["generation_mode"], "catalog generation") != 0o600
        or _mode(data["state_mode"], "catalog state") != 0o600
    ):
        raise UpgradeHostAError("Host A catalog artifact mode is invalid")
    return CatalogObservation(
        _sha(data["applied_model_set_sha256"], "applied model set"),
        _path(data["generation_path"], "catalog generation"),
        _sha(data["generation_sha256"], "catalog generation"),
        _sha(data["state_output_sha256"], "catalog state output"),
        _path(data["state_path"], "catalog state"),
    )


def _optional_schedule(value: proof.JsonValue, root: Path) -> ScheduleObservation | None:
    if value is None:
        return None
    data = _mapping(value, "schedule")
    _keys(
        data,
        {
            "applied_desired_sha256",
            "desired_sha256",
            "desired_spec_sha256",
            "receipt_mode",
            "receipt_path",
            "receipt_sha256",
            "remote_spec_sha256",
        },
        "schedule",
    )
    receipt_sha256 = _sha(data["receipt_sha256"], "schedule receipt")
    receipt_path = (
        root / "state" / "shared-core-sync" / "schedule-receipts" / f"{receipt_sha256}.json"
    )
    if (
        _path(data["receipt_path"], "schedule receipt") != receipt_path
        or _mode(data["receipt_mode"], "schedule receipt") != 0o600
    ):
        raise UpgradeHostAError("Host A schedule receipt path or mode is invalid")
    return ScheduleObservation(
        _sha(data["desired_sha256"], "schedule desired"),
        _sha(data["applied_desired_sha256"], "schedule applied"),
        _sha(data["desired_spec_sha256"], "schedule desired spec"),
        _sha(data["remote_spec_sha256"], "schedule remote spec"),
        receipt_sha256,
    )


def _sync_results(value: proof.JsonValue) -> tuple[SyncResultObservation, ...]:
    if not isinstance(value, list):
        raise UpgradeHostAError("Host A synchronizer results are malformed")
    results = tuple(_sync_result(_mapping(item, "sync result")) for item in value)
    digests = tuple(item.result_sha256 for item in results)
    if digests != tuple(sorted(set(digests))):
        raise UpgradeHostAError("Host A synchronizer results are not canonical")
    return results


def _sync_result(data: dict[str, proof.JsonValue]) -> SyncResultObservation:
    _keys(data, {"durable_write_count", "mode", "path", "result_sha256"}, "sync result")
    count = data["durable_write_count"]
    if type(count) is not int or count < 0 or _mode(data["mode"], "sync result") != 0o600:
        raise UpgradeHostAError("Host A synchronizer result count or mode is invalid")
    return SyncResultObservation(
        _sha(data["result_sha256"], "sync result"),
        count,
        _path(data["path"], "sync result"),
    )


def _mapping(value: proof.JsonValue, label: str) -> dict[str, proof.JsonValue]:
    try:
        return proof.require_mapping(value, label)
    except ValueError as error:
        raise UpgradeHostAError(f"Host A {label} observation is malformed") from error


def _keys(data: Mapping[str, proof.JsonValue], expected: set[str], label: str) -> None:
    if set(data) != expected:
        raise UpgradeHostAError(f"Host A {label} observation fields are invalid")


def _sha(value: proof.JsonValue, label: str) -> str:
    text = _text(value, label)
    if _SHA256.fullmatch(text) is None:
        raise UpgradeHostAError(f"Host A {label} digest is invalid")
    return text


def _text(value: proof.JsonValue, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise UpgradeHostAError(f"Host A {label} is invalid")
    return value


def _path(value: proof.JsonValue, label: str) -> Path:
    path = Path(_text(value, label))
    if not path.is_absolute() or path != Path(path.as_posix()):
        raise UpgradeHostAError(f"Host A {label} path is invalid")
    return path


def _mode(value: proof.JsonValue, label: str) -> int:
    if type(value) is not int:
        raise UpgradeHostAError(f"Host A {label} mode is invalid")
    return value


def _text_sequence(value: proof.JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise UpgradeHostAError(f"Host A {label} are malformed")
    result = tuple(_text(item, label) for item in value)
    if result != tuple(sorted(set(result))):
        raise UpgradeHostAError(f"Host A {label} are not canonical")
    return result


def _sha_sequence(value: proof.JsonValue, label: str) -> tuple[str, ...]:
    result = _text_sequence(value, label)
    if any(_SHA256.fullmatch(item) is None for item in result):
        raise UpgradeHostAError(f"Host A {label} contain an invalid digest")
    return result


def _migration_status(value: proof.JsonValue) -> MigrationStatus:
    status = _text(value, "migration status")
    parsed = _MIGRATION_STATUSES.get(status)
    if parsed is None:
        raise UpgradeHostAError("Host A migration status is invalid")
    return parsed
