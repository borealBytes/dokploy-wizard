from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Final
from uuid import UUID

from dokploy_wizard.state.sync_schema import JsonValue

_MAX_BYTES: Final = 8 * 1024
_PROVENANCE: Final = {
    "baseline_sha256": "f9ffdaf99a88ca9472d24aa1cfeaa3ce305fee826d20df61e7bece956803caf9",
    "result_sha256": "e73989d8493c538306cd5827f0c53e870a6fc62d7937431aca6d5166ed9af3a4",
    "lifecycle_sha256": "1e9d5ee1fa269e35bdb7ee3baceab196cc45296aae963cc52e4b3f6067b468f9",
    "proof_commit": "04e4ad830f72ad08e0e2d3e019cb3d375aa76ddb",
    "coder_secret_inventory_sha256": (
        "5c091fcae52dc3f9e2a36fd9522af1a5dd66f649cde16214606d8df4479e25e1"
    ),
}
_KEYS: Final = frozenset(
    (
        *_PROVENANCE,
        "clean_before_namespace_absent",
        "host_identity_mode",
        "post_install_phase",
        "records",
        "schema_version",
    )
)
_RECORD_KEYS: Final = frozenset(("secret_id", "secret_name", "env_name", "description"))


@dataclass(frozen=True, slots=True)
class Task1CoderSecretAttestationError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


def proves_task1_created_secret(
    *, secret_id: str, name: str, env_name: str, description: str
) -> bool:
    return (secret_id, name, env_name, description) in _load()


def _load() -> frozenset[tuple[str, str, str, str]]:
    try:
        raw = files("dokploy_wizard.dokploy").joinpath("task1_coder_secret_lock.json").read_bytes()
    except OSError as error:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret lock is unavailable") from error
    if len(raw) > _MAX_BYTES:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret lock is oversized")
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError as error:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret lock is malformed") from error
    mapping = _mapping(value, _KEYS, "lock")
    _require(
        mapping["schema_version"] == 1 and not isinstance(mapping["schema_version"], bool),
        "version is invalid",
    )
    for key, expected in _PROVENANCE.items():
        _require(mapping[key] == expected, "provenance is invalid")
    _require(
        mapping["clean_before_namespace_absent"] is True
        and mapping["host_identity_mode"] == "single_sequential"
        and mapping["post_install_phase"] == "fresh_install",
        "lifecycle is invalid",
    )
    records_value = mapping["records"]
    if not isinstance(records_value, list) or len(records_value) != 4:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret records are invalid")
    records = tuple(_record(value) for value in records_value)
    if tuple(sorted(records, key=lambda record: record[0])) != records:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret records are not canonical")
    if len({record[0] for record in records}) != len(records):
        raise Task1CoderSecretAttestationError("Task 1 Coder secret records are duplicated")
    inventory = [
        {
            "id": secret_id,
            "name": name,
            "environment_variable": env_name,
            "description": description,
        }
        for secret_id, name, env_name, description in records
    ]
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    if sha256(encoded).hexdigest() != _PROVENANCE["coder_secret_inventory_sha256"]:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret inventory is invalid")
    return frozenset(records)


def _record(value: JsonValue) -> tuple[str, str, str, str]:
    mapping = _mapping(value, _RECORD_KEYS, "record")
    secret_id = _uuid(mapping["secret_id"])
    return (
        secret_id,
        _text(mapping["secret_name"], "record"),
        _text(mapping["env_name"], "record"),
        _text(mapping["description"], "record"),
    )


def _mapping(value: JsonValue, keys: frozenset[str], label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise Task1CoderSecretAttestationError(f"Task 1 Coder secret {label} is invalid")
    return value


def _uuid(value: JsonValue) -> str:
    text = _text(value, "record")
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret record is invalid") from error
    if str(parsed) != text:
        raise Task1CoderSecretAttestationError("Task 1 Coder secret record is invalid")
    return text


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Task1CoderSecretAttestationError(f"Task 1 Coder secret {label} is invalid")
    return value


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Task1CoderSecretAttestationError(f"Task 1 Coder secret lock {reason}")
