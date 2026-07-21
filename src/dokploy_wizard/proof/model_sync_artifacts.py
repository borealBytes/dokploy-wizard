"""Redacted, fsync-backed manifest helpers for live proof evidence."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias
from urllib.parse import urlsplit

from dokploy_wizard.verification import redact_text

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
_HASH_PATTERNS: Final = (re.compile(r"^[0-9a-f]{64}$"), re.compile(r"^sha256:[0-9a-f]{64}$"))
_REPOSITORY: Final = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$")  # noqa: E501
_MANIFEST_MEDIA: Final = frozenset({"application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"})  # noqa: E501
_INDEX_MEDIA: Final = frozenset({"application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.oci.image.index.v1+json"})  # noqa: E501
_FILE_MODE: Final = 0o600
def sha256_bytes(value: bytes) -> str:
    """Return a stable fingerprint for non-secret artifact bytes."""
    return hashlib.sha256(value).hexdigest()
def atomic_write_bytes(path: Path, content: bytes, *, mode: int = _FILE_MODE) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            _write_all(descriptor, content)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(descriptor, view) :]
def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
@dataclass(frozen=True, slots=True)
class CaptureSchemaError(RuntimeError):
    """Raised when a remote capture cannot be safely normalized."""
    detail: str
    def __str__(self) -> str:
        return self.detail
@dataclass(frozen=True, slots=True)
class ResourcePlaneCapture:
    """Normalized non-secret resource planes captured after the wrapper succeeds."""
    cloudflare: dict[str, JsonValue]
    images: dict[str, str]
    tailscale: dict[str, JsonValue]
    wizard_state: dict[str, JsonValue]
def write_protected_manifest(path: Path, payload: Mapping[str, JsonValue]) -> str:
    redacted = _redact_mapping(payload)
    encoded = (json.dumps(redacted, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes(path, encoded)
    return sha256_bytes(encoded)
def read_protected_manifest(path: Path) -> dict[str, JsonValue]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("protected manifest must be a JSON object")
    return _redact_mapping(decoded)
def redact_manifest_value(key: str, value: JsonValue) -> JsonValue:
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
def require_mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    """Return a strictly JSON-compatible object or reject the capture boundary."""
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise CaptureSchemaError(f"{label} must be an object")
    return dict(value)
def require_list(value: JsonValue, label: str) -> list[JsonValue]:
    """Return a JSON array or reject the capture boundary."""
    if not isinstance(value, list):
        raise CaptureSchemaError(f"{label} must be an array")
    return list(value)
def require_keys(value: dict[str, JsonValue], expected: set[str], label: str) -> None:
    """Reject unknown, missing, and value-bearing schema fields."""
    if set(value) != expected:
        raise CaptureSchemaError(f"{label} keys are invalid")
def require_text(value: JsonValue, label: str) -> str:
    """Reject empty and non-string remote fields."""
    if not isinstance(value, str) or value == "":
        raise CaptureSchemaError(f"{label} must be a non-empty string")
    return value
def require_safe_base_url(value: str) -> str:
    if any(character.isspace() or not character.isprintable() for character in value):
        raise CaptureSchemaError("legacy pointer base URL is unsafe")
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise CaptureSchemaError("legacy pointer base URL is unsafe") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or any(segment in {".", ".."} for segment in parsed.path.split("/")):  # noqa: E501
        raise CaptureSchemaError("legacy pointer base URL is unsafe")
    return value
def require_sha256(value: JsonValue, label: str) -> str:
    """Return a non-placeholder SHA-256 capture fingerprint."""
    text = require_text(value, label)
    if not _HASH_PATTERNS[0].fullmatch(text) or text == "0" * 64:
        raise CaptureSchemaError(f"{label} must be a non-zero SHA-256")
    return text
def require_digest(value: JsonValue, label: str) -> str:
    """Return a resolved immutable container image reference."""
    text = require_text(value, label)
    if text.count("@") != 1:
        raise CaptureSchemaError(f"{label} must use repository@sha256")
    repository, digest = text.split("@")
    if not _HASH_PATTERNS[1].fullmatch(digest):
        raise CaptureSchemaError(f"{label} must use repository@sha256")
    return f"{normalize_image_repository(repository, label)}@{digest}"
def normalize_image_repository(reference: str, label: str) -> str:
    if any(character.isspace() or not character.isprintable() for character in reference):
        raise CaptureSchemaError(f"{label} repository is invalid")
    base = reference.partition("@")[0]
    last_slash, last_colon = base.rfind("/"), base.rfind(":")
    if last_colon > last_slash:
        base = base[:last_colon]
    parts = base.split("/")
    if len(parts) == 1:
        base = f"docker.io/library/{base}"
    elif "." not in parts[0] and ":" not in parts[0] and parts[0] != "localhost":
        base = f"docker.io/{base}"
    elif parts[0] == "index.docker.io":
        base = "/".join(("docker.io", *parts[1:]))
    if not base or base != base.lower() or _REPOSITORY.fullmatch(base) is None:
        raise CaptureSchemaError(f"{label} repository is invalid")
    return base
def registry_manifest_digest(raw: bytes, repository: str, label: str) -> str:
    """Validate documented raw manifest/index JSON and hash the registry bytes."""
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaptureSchemaError(f"{label} returned invalid JSON") from error
    manifest = require_mapping(decoded, label)
    if manifest.get("schemaVersion") != 2:
        raise CaptureSchemaError(f"{label} schema version is unsupported")
    media_type = require_text(manifest.get("mediaType"), f"{label} media type")
    if media_type in _MANIFEST_MEDIA:
        values = [manifest.get("config"), *require_list(manifest.get("layers"), f"{label} layers")]
    elif media_type in _INDEX_MEDIA:
        values = require_list(manifest.get("manifests"), f"{label} manifests")
        if not values:
            raise CaptureSchemaError(f"{label} manifests must not be empty")
    else:
        raise CaptureSchemaError(f"{label} media type is unsupported")
    for value in values:
        descriptor = require_mapping(value, f"{label} descriptor")
        require_text(descriptor.get("mediaType"), f"{label} descriptor media type")
        if _HASH_PATTERNS[1].fullmatch(require_text(descriptor.get("digest"), f"{label} descriptor digest")) is None:  # noqa: E501
            raise CaptureSchemaError(f"{label} descriptor digest is invalid")
        size = descriptor.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CaptureSchemaError(f"{label} descriptor size is invalid")
    return f"{repository}@sha256:{hashlib.sha256(raw).hexdigest()}"
def protected_manifest_bytes(entries: Mapping[str, str]) -> bytes:
    """Render a real text manifest from exact protected-artifact hashes."""
    lines: list[str] = []
    for path, fingerprint in sorted(entries.items()):
        require_sha256(fingerprint, f"manifest fingerprint for {path}")
        if path == "" or "\n" in path:
            raise CaptureSchemaError("protected manifest path is invalid")
        lines.append(f"{fingerprint}  {path}\n")
    if not lines:
        raise CaptureSchemaError("protected manifest must not be empty")
    return "".join(lines).encode("utf-8")
def finalize_capture_outputs(outputs: Mapping[Path, bytes]) -> None:
    """Finalize a complete capture set or remove every newly written artifact on failure."""
    if not outputs:
        raise CaptureSchemaError("capture outputs must not be empty")
    if any(path.exists() for path in outputs):
        raise CaptureSchemaError("capture output already exists")
    try:
        for path, content in sorted(outputs.items(), key=lambda item: str(item[0])):
            atomic_write_bytes(path, content)
    except BaseException:
        for path in outputs:
            path.unlink(missing_ok=True)
        raise
def parse_resource_planes(snapshot: dict[str, JsonValue]) -> ResourcePlaneCapture:
    """Reject incomplete container, state, Cloudflare, and Tailscale inventories."""
    images: dict[str, str] = {}
    for value in require_list(snapshot["images"], "images"):
        image = require_mapping(value, "image")
        require_keys(image, {"logical_name", "container_image", "registry_image"}, "image")
        name = require_text(image["logical_name"], "image logical name")
        container = require_digest(image["container_image"], "container image")
        if container != require_digest(image["registry_image"], "registry image"):
            raise CaptureSchemaError("container and registry image digests disagree")
        if name in images:
            raise CaptureSchemaError("captured image logical names must be unique")
        images[name] = container
    if set(images) != {"coder", "litellm", "pgvector", "redis", "postfix"}:
        raise CaptureSchemaError("all required Coder and Shared Core images must be captured")
    state = require_mapping(snapshot["wizard_state"], "wizard state")
    require_keys(state, {"state_sha256", "ledger_sha256", "resources"}, "wizard state")
    resources = _unique_strings(require_list(state["resources"], "wizard resources"), "wizard resource")  # noqa: E501
    return ResourcePlaneCapture(
        cloudflare=_identifiers(snapshot["cloudflare"], "cloudflare", {"tunnel_ids", "dns_record_ids", "access_application_ids"}),  # noqa: E501
        images=images,
        tailscale=_identifiers(snapshot["tailscale"], "tailscale", {"identifiers"}),
        wizard_state={"ledger_sha256": require_sha256(state["ledger_sha256"], "wizard ledger"), "resources": sorted(resources), "state_sha256": require_sha256(state["state_sha256"], "wizard state")},  # noqa: E501
    )
def _identifiers(raw: JsonValue, label: str, expected: set[str]) -> dict[str, JsonValue]:
    source = require_mapping(raw, label)
    require_keys(source, expected, label)
    return {key: sorted(_unique_strings(require_list(source[key], f"{label}.{key}"), f"{label}.{key}")) for key in sorted(expected)}  # noqa: E501
def _unique_strings(values: list[JsonValue], label: str) -> list[str]:
    result = [require_text(value, label) for value in values]
    if len(result) != len(set(result)):
        raise CaptureSchemaError(f"{label} identifiers must be unique")
    return result
