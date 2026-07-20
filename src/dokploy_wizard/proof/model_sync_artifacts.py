"""Redacted, fsync-backed manifest helpers for live proof evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias

from dokploy_wizard.proof.model_sync_state import atomic_write_bytes, sha256_bytes
from dokploy_wizard.verification import redact_text

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def write_protected_manifest(path: Path, payload: Mapping[str, JsonValue]) -> str:
    """Write a redacted mode-0600 manifest and return its content fingerprint."""
    redacted = _redact_mapping(payload)
    encoded = (json.dumps(redacted, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes(path, encoded)
    return sha256_bytes(encoded)


def read_protected_manifest(path: Path) -> dict[str, JsonValue]:
    """Read a manifest only when it is a JSON object with no raw secret fields."""
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("protected manifest must be a JSON object")
    return _redact_mapping(decoded)


def redact_manifest_value(key: str, value: JsonValue) -> JsonValue:
    """Retain fingerprints while replacing values in secret-bearing fields."""
    normalized = key.lower()
    if normalized.endswith("_sha256") or normalized.endswith("_digest"):
        return value
    secret_markers = ("password", "token", "secret", "credential", "api_key", "salt")
    if any(token in normalized for token in secret_markers):
        return "<REDACTED>"
    match value:
        case str() as text:
            return redact_text(text)
        case list() as items:
            return [_redact_item(item) for item in items]
        case dict() as nested:
            return _redact_mapping(nested)
        case _:
            return value


def _redact_mapping(payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: redact_manifest_value(key, value) for key, value in sorted(payload.items())}


def _redact_item(value: JsonValue) -> JsonValue:
    match value:
        case str() as text:
            return redact_text(text)
        case list() as items:
            return [_redact_item(item) for item in items]
        case dict() as nested:
            return _redact_mapping(nested)
        case _:
            return value
