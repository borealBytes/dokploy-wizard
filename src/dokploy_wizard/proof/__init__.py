# ruff: noqa: E501
"""Crash-safe proof tooling for the Coder/LiteLLM model-sync baseline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from typing import Final, Literal, NamedTuple, TypeAlias
from urllib.parse import urlsplit

from dokploy_wizard.verification import redact_text

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GUARD_FILE_MODE: Final = 0o600
_MAX_GUARD_BYTES: Final = 256 * 1024
_READ_CHUNK_BYTES: Final = 64 * 1024
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
ProtectedManifestEntry = NamedTuple(
    "ProtectedManifestEntry",
    [("path", PurePosixPath), ("sha256", str)],
)
_HASH_PATTERNS: Final = (
    re.compile(r"^[0-9a-f]{64}$"),
    re.compile(r"^sha256:[0-9a-f]{64}$"),
)
_REPOSITORY: Final = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
_MANIFEST_MEDIA: Final = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
_INDEX_MEDIA: Final = frozenset(
    {
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    }
)
_PROTECTED_FORBIDDEN: Final = (
    "password",
    "token",
    "credential",
    "api_key",
    ".install",
    "secret",
    "protected-artifacts-before",
    ".tmp",
    ".lock",
    "coder-litellm-model-sync",
    "run-continuation",
    "start-work",
    "boulder.json",
    "plans/assets/kdense-",
    "/baseline.json",
    "/result.json",
    "/abort-guard.json",
    "/abort-status.json",
    "/host-a-preflight.json",
    "/host-b-preflight.json",
)
REQUIRED_RESULT_KEYS: Final = frozenset(
    {
        "abort_guard_path",
        "abort_guard_sha256",
        "baseline_sha256",
        "coder_image_digest",
        "coder_secret_inventory_sha256",
        "env_mode",
        "env_original_sha256",
        "env_proof_sha256",
        "external_backup_path",
        "host_a_preflight_sha256",
        "host_architectures_equal",
        "host_b_preflight_sha256",
        "host_identities_distinct",
        "legacy_workspace_managed_fingerprints_sha256",
        "litellm_image_digest",
        "proof_commit",
        "protected_artifacts_before_path",
        "protected_artifacts_before_sha256",
        "schema_version",
        "shared_core_image_digests",
        "source_base_commit",
    }
)


@dataclass(frozen=True, slots=True)
class CaptureSchemaError(RuntimeError):
    detail: str

    def __str__(self) -> str:
        return self.detail


class AbortGuardError(RuntimeError):
    """Raised when durable GuardV3 evidence is invalid or unauthorized."""


@dataclass(frozen=True, slots=True)
class ResourcePlaneCapture:
    cloudflare: dict[str, JsonValue]
    images: dict[str, str]
    tailscale: dict[str, JsonValue]
    wizard_state: dict[str, JsonValue]


def redact_manifest_value(key: str, value: JsonValue) -> JsonValue:
    normalized = key.lower()
    if normalized.endswith(("_sha256", "_digest")):
        return value
    if any(
        token in normalized
        for token in ("password", "token", "secret", "credential", "api_key", "salt")
    ):
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


def protected_manifest_bytes(entries: Mapping[str, str]) -> bytes:
    lines: list[str] = []
    for path, fingerprint in sorted(entries.items()):
        require_sha256(fingerprint, f"manifest fingerprint for {path}")
        if path == "" or "\n" in path:
            raise CaptureSchemaError("protected manifest path is invalid")
        lines.append(f"{fingerprint}  {path}\n")
    if not lines:
        raise CaptureSchemaError("protected manifest must not be empty")
    return "".join(lines).encode("utf-8")


def validate_protected_manifest_bytes(content: bytes) -> tuple[ProtectedManifestEntry, ...]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise CaptureSchemaError("protected manifest encoding is invalid") from error
    entries: dict[str, str] = {}
    for line in lines:
        fingerprint, separator, path = line.partition("  ")
        normalized = PurePosixPath(path).as_posix()
        duplicate = normalized in entries
        if (
            duplicate
            or separator != "  "
            or not normalized.startswith((".omo/", ".sisyphus/"))
            or path != normalized
            or "" in path.split("/")
            or "\\" in path
            or any(part in {".", ".."} for part in PurePosixPath(path).parts)
            or any(not character.isprintable() or character.isspace() for character in path)
            or any(marker in path.lower() for marker in _PROTECTED_FORBIDDEN)
        ):
            detail = (
                "protected manifest duplicate normalized path"
                if duplicate
                else "protected manifest path is unsafe"
            )
            raise CaptureSchemaError(detail)
        entries[normalized] = require_sha256(fingerprint, f"manifest fingerprint for {path}")
    if content != protected_manifest_bytes(entries):
        raise CaptureSchemaError("protected manifest bytes are not canonical")
    return tuple(
        ProtectedManifestEntry(PurePosixPath(path), fingerprint)
        for path, fingerprint in entries.items()
    )


def require_mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise CaptureSchemaError(f"{label} must be an object")
    return dict(value)


def require_list(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise CaptureSchemaError(f"{label} must be an array")
    return list(value)


def require_keys(value: dict[str, JsonValue], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CaptureSchemaError(f"{label} keys are invalid")


def require_text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise CaptureSchemaError(f"{label} must be a non-empty string")
    return value


def require_safe_base_url(value: str) -> str:
    if (
        not value.isascii()
        or not value.startswith(("http://", "https://"))
        or "%" in value
        or "\\" in value
        or any(character.isspace() or not character.isprintable() for character in value)
    ):
        raise CaptureSchemaError("legacy pointer base URL is unsafe")
    try:
        parsed = urlsplit(value)
        port, host = parsed.port, parsed.hostname or ""
        address = ip_address(host) if ":" in host or host.replace(".", "").isdigit() else None
    except ValueError as error:
        raise CaptureSchemaError("legacy pointer base URL is unsafe") from error
    canonical_host = (
        f"[{address.compressed}]"
        if address is not None and address.version == 6
        else str(address)
        if address is not None
        else host
    )
    expected_authority = canonical_host + (f":{port}" if port is not None else "")
    path = parsed.path
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != expected_authority
        or (port is not None and (port == 0 or port == (80 if parsed.scheme == "http" else 443)))
        or (
            address is None
            and (
                len(host) > 253
                or any(
                    label.startswith("xn--")
                    or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                    for label in host.split(".")
                )
            )
        )
        or (
            path
            and (
                not path.startswith("/")
                or path != PurePosixPath(path).as_posix()
                or "" in path[1:].split("/")
                or any(segment in {".", ".."} for segment in path.split("/"))
            )
        )
    ):
        raise CaptureSchemaError("legacy pointer base URL is unsafe")
    return value


def require_sha256(value: JsonValue, label: str) -> str:
    text = require_text(value, label)
    if not _HASH_PATTERNS[0].fullmatch(text) or text == "0" * 64:
        raise CaptureSchemaError(f"{label} must be a non-zero SHA-256")
    return text


def require_digest(value: JsonValue, label: str) -> str:
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
        digest = require_text(descriptor.get("digest"), f"{label} descriptor digest")
        if _HASH_PATTERNS[1].fullmatch(digest) is None:
            raise CaptureSchemaError(f"{label} descriptor digest is invalid")
        size = descriptor.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CaptureSchemaError(f"{label} descriptor size is invalid")
    return f"{repository}@sha256:{hashlib.sha256(raw).hexdigest()}"


def parse_resource_planes(snapshot: dict[str, JsonValue]) -> ResourcePlaneCapture:
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
    resources = _unique_strings(
        require_list(state["resources"], "wizard resources"),
        "wizard resource",
    )
    return ResourcePlaneCapture(
        cloudflare=_identifiers(
            snapshot["cloudflare"],
            "cloudflare",
            {"tunnel_ids", "dns_record_ids", "access_application_ids"},
        ),
        images=images,
        tailscale=_identifiers(snapshot["tailscale"], "tailscale", {"identifiers"}),
        wizard_state={
            "ledger_sha256": require_sha256(state["ledger_sha256"], "wizard ledger"),
            "resources": sorted(resources),
            "state_sha256": require_sha256(state["state_sha256"], "wizard state"),
        },
    )


def _identifiers(raw: JsonValue, label: str, expected: set[str]) -> dict[str, JsonValue]:
    source = require_mapping(raw, label)
    require_keys(source, expected, label)
    return {
        key: sorted(
            _unique_strings(
                require_list(source[key], f"{label}.{key}"),
                f"{label}.{key}",
            )
        )
        for key in sorted(expected)
    }


def _unique_strings(values: list[JsonValue], label: str) -> list[str]:
    result = [require_text(value, label) for value in values]
    if len(result) != len(set(result)):
        raise CaptureSchemaError(f"{label} identifiers must be unique")
    return result


GuardState = Literal["armed", "disarmed"]
GuardPhase = Literal[
    "ready",
    "claimed",
    "env_intent",
    "proof_active",
    "finalize_intent",
    "rollback",
    "complete",
]
ClaimantKind = Literal["plan", "process"]


@dataclass(frozen=True, slots=True)
class EnvReceipt:
    env_path: str
    backup_path: str
    original_sha256: str
    proof_sha256: str
    mode: int

    def to_payload(self) -> dict[str, str | int]:
        return {
            "backup_path": self.backup_path,
            "env_path": self.env_path,
            "mode": self.mode,
            "original_sha256": self.original_sha256,
            "proof_sha256": self.proof_sha256,
            "schema_version": 1,
        }


@dataclass(frozen=True, slots=True)
class BaselineAttestation:
    guard_id: str
    guard_path: str
    artifact_dir: str
    result_path: str
    env_receipt: EnvReceipt
    output_sha256: Mapping[str, str]
    result_body: Mapping[str, JsonValue]

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "artifact_dir": self.artifact_dir,
            "env_receipt": self.env_receipt.to_payload(),
            "guard_id": self.guard_id,
            "guard_path": self.guard_path,
            "kind": "task-1-baseline-finalization",
            "output_sha256": dict(self.output_sha256),
            "required_terminal": {
                "claimant_kind": "plan",
                "phase": "complete",
                "state": "armed",
            },
            "result_body": dict(self.result_body),
            "result_path": self.result_path,
            "schema_version": 1,
        }


@dataclass(frozen=True, slots=True)
class AbortGuard:
    guard_id: str
    state: GuardState
    phase: GuardPhase
    claimant_kind: ClaimantKind
    pid: int | None
    start_time_ticks: str | None
    claim_token: str | None
    env_receipt: EnvReceipt | None
    attestation: BaselineAttestation | None


def canonical_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def read_guard_bytes(path: Path) -> bytes:
    before = os.lstat(path)
    if not _authorized_guard_metadata(before) or before.st_size > _MAX_GUARD_BYTES:
        raise ValueError("abort guard must be a bounded mode-0600 regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if _guard_metadata(before) != _guard_metadata(opened):
            raise ValueError("abort guard metadata changed before reading")
        content = bytearray()
        while len(content) <= opened.st_size:
            request = min(_READ_CHUNK_BYTES, opened.st_size + 1 - len(content))
            chunk = os.read(descriptor, request)
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        pathname = os.lstat(path)
        if (
            len(content) != opened.st_size
            or _guard_metadata(opened) != _guard_metadata(after)
            or _guard_metadata(opened) != _guard_metadata(pathname)
        ):
            raise ValueError("abort guard changed while reading")
        return bytes(content)
    finally:
        os.close(descriptor)


def parse_env_receipt(value: JsonValue | None) -> EnvReceipt | None:
    if value is None:
        return None
    expected = {
        "backup_path",
        "env_path",
        "mode",
        "original_sha256",
        "proof_sha256",
        "schema_version",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("abort guard env receipt is invalid")
    mode = value["mode"]
    version = value["schema_version"]
    if (
        isinstance(version, bool)
        or version != 1
        or isinstance(mode, bool)
        or not isinstance(mode, int)
        or not 1 <= mode <= 0o777
    ):
        raise ValueError("abort guard env receipt is invalid")
    return EnvReceipt(
        _path(value["env_path"]),
        _path(value["backup_path"]),
        _hash(value["original_sha256"]),
        _hash(value["proof_sha256"]),
        mode,
    )


def parse_baseline_attestation(value: JsonValue | None) -> BaselineAttestation | None:
    if value is None:
        return None
    expected = {
        "artifact_dir",
        "env_receipt",
        "guard_id",
        "guard_path",
        "kind",
        "output_sha256",
        "required_terminal",
        "result_body",
        "result_path",
        "schema_version",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != 1
        or value["kind"] != "task-1-baseline-finalization"
    ):
        raise ValueError("abort guard attestation is invalid")
    receipt = parse_env_receipt(value["env_receipt"])
    outputs = value["output_sha256"]
    body = value["result_body"]
    if (
        receipt is None
        or value["required_terminal"]
        != {"state": "armed", "phase": "complete", "claimant_kind": "plan"}
        or not isinstance(outputs, dict)
        or not isinstance(body, dict)
    ):
        raise ValueError("abort guard attestation is invalid")
    attestation = BaselineAttestation(
        _hash(value["guard_id"]),
        _path(value["guard_path"]),
        _path(value["artifact_dir"]),
        _path(value["result_path"]),
        receipt,
        {key: _hash(item) for key, item in outputs.items()},
        body,
    )
    validate_baseline_attestation_bindings(attestation)
    return attestation


def validate_baseline_attestation_bindings(attestation: BaselineAttestation) -> None:
    _hash(attestation.guard_id)
    guard_path = _path(attestation.guard_path)
    artifact_dir = _path(attestation.artifact_dir)
    result_path = _path(attestation.result_path)
    receipt = parse_env_receipt(attestation.env_receipt.to_payload())
    if receipt is None:
        raise ValueError("abort guard attestation receipt is invalid")
    expected_outputs = {
        "baseline.json",
        "host-a-preflight.json",
        "host-b-preflight.json",
        "protected-artifacts-before.txt",
    }
    if set(attestation.output_sha256) != expected_outputs:
        raise ValueError("abort guard attestation outputs are invalid")
    outputs = {
        key: _hash(value)
        for key, value in attestation.output_sha256.items()
    }
    body = attestation.result_body
    if frozenset(body) != REQUIRED_RESULT_KEYS - frozenset({"abort_guard_sha256"}):
        raise ValueError("abort guard attestation result keys are invalid")
    expected_bindings: dict[str, JsonValue] = {
        "abort_guard_path": guard_path,
        "baseline_sha256": outputs["baseline.json"],
        "env_mode": receipt.mode,
        "env_original_sha256": receipt.original_sha256,
        "env_proof_sha256": receipt.proof_sha256,
        "external_backup_path": receipt.backup_path,
        "host_a_preflight_sha256": outputs["host-a-preflight.json"],
        "host_b_preflight_sha256": outputs["host-b-preflight.json"],
        "protected_artifacts_before_path": f"{artifact_dir}/protected-artifacts-before.txt",
        "protected_artifacts_before_sha256": outputs["protected-artifacts-before.txt"],
    }
    if (
        result_path != f"{artifact_dir}/result.json"
        or any(body[key] != value for key, value in expected_bindings.items())
    ):
        raise ValueError("abort guard attestation bindings are invalid")


def parse_abort_guard(value: JsonValue) -> AbortGuard:
    expected = {
        "attestation",
        "claim_token",
        "claimant_kind",
        "env_receipt",
        "guard_id",
        "phase",
        "pid",
        "schema_version",
        "start_time_ticks",
        "state",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["schema_version"] != 3
    ):
        raise ValueError("abort guard schema is invalid")
    pid = value["pid"]
    start_time = value["start_time_ticks"]
    claim_token = value["claim_token"]
    if (
        (pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)))
        or (start_time is not None and not isinstance(start_time, str))
        or (claim_token is not None and not isinstance(claim_token, str))
    ):
        raise ValueError("abort guard ownership fields are invalid")
    guard = AbortGuard(
        _hash(value["guard_id"]),
        _guard_state(value["state"]),
        _guard_phase(value["phase"]),
        _claimant_kind(value["claimant_kind"]),
        pid,
        start_time,
        claim_token,
        parse_env_receipt(value["env_receipt"]),
        parse_baseline_attestation(value["attestation"]),
    )
    validate_abort_guard(guard)
    return guard


def validate_abort_guard(guard: AbortGuard) -> None:
    process_fields = (guard.pid, guard.start_time_ticks, guard.claim_token)
    if guard.claimant_kind == "process":
        if not valid_process_identity(*process_fields):
            raise ValueError("abort guard process identity is invalid")
    elif any(value is not None for value in process_fields):
        raise ValueError("abort guard plan ownership fields are invalid")
    has_receipt = guard.env_receipt is not None
    has_attestation = guard.attestation is not None
    combination = (
        guard.state,
        guard.phase,
        guard.claimant_kind,
        has_receipt,
        has_attestation,
    )
    legal = {
        ("armed", "ready", "plan", False, False),
        ("armed", "claimed", "process", False, False),
        ("armed", "env_intent", "process", True, False),
        ("armed", "proof_active", "process", True, False),
        ("armed", "finalize_intent", "process", True, True),
        ("armed", "rollback", "plan", False, False),
        ("armed", "rollback", "plan", True, False),
        ("armed", "rollback", "plan", True, True),
        ("armed", "rollback", "process", False, False),
        ("armed", "rollback", "process", True, False),
        ("armed", "rollback", "process", True, True),
        ("armed", "complete", "plan", True, True),
        ("disarmed", "complete", "plan", True, True),
    }
    if combination not in legal:
        raise ValueError("abort guard phase combination is invalid")
    if guard.attestation is not None and (
        guard.env_receipt is None
        or guard.attestation.guard_id != guard.guard_id
        or receipt_identity(guard.attestation.env_receipt)
        != receipt_identity(guard.env_receipt)
    ):
        raise ValueError("abort guard attestation binding is invalid")


def abort_guard_payload(guard: AbortGuard) -> dict[str, JsonValue]:
    return {
        "attestation": None if guard.attestation is None else guard.attestation.to_payload(),
        "claim_token": guard.claim_token,
        "claimant_kind": guard.claimant_kind,
        "env_receipt": receipt_payload(guard.env_receipt),
        "guard_id": guard.guard_id,
        "phase": guard.phase,
        "pid": guard.pid,
        "schema_version": 3,
        "start_time_ticks": guard.start_time_ticks,
        "state": guard.state,
    }


def process_start_time_ticks(stat_text: str) -> str:
    closing = stat_text.rfind(")")
    fields = stat_text[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise AbortGuardError("proc stat is malformed")
    return fields[19]


def process_identity_matches(pid: int, start_time_ticks: str) -> bool:
    try:
        actual = process_start_time_ticks(
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        )
    except OSError:
        return False
    return actual == start_time_ticks


def valid_process_identity(
    pid: int | None,
    start_time_ticks: str | None,
    claim_token: str | None,
) -> bool:
    return (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(start_time_ticks, str)
        and start_time_ticks.isdecimal()
        and isinstance(claim_token, str)
        and len(claim_token) == 32
        and all(character.isalnum() or character in "_-" for character in claim_token)
    )


def receipt_identity(receipt: EnvReceipt) -> tuple[str, str, str, str, int]:
    return (
        receipt.env_path,
        receipt.backup_path,
        receipt.original_sha256,
        receipt.proof_sha256,
        receipt.mode,
    )


def receipt_payload(receipt: EnvReceipt | None) -> dict[str, str | int] | None:
    return None if receipt is None else receipt.to_payload()


def abort_guard_sha256(attestation: BaselineAttestation) -> str:
    return hashlib.sha256(canonical_json_bytes(attestation.to_payload())).hexdigest()


def _guard_state(value: JsonValue) -> GuardState:
    match value:
        case "armed":
            return "armed"
        case "disarmed":
            return "disarmed"
        case _:
            raise ValueError("abort guard state is invalid")


def _guard_phase(value: JsonValue) -> GuardPhase:
    match value:
        case (
            "ready"
            | "claimed"
            | "env_intent"
            | "proof_active"
            | "finalize_intent"
            | "rollback"
            | "complete"
        ) as phase:
            return phase
        case _:
            raise ValueError("abort guard phase is invalid")


def _claimant_kind(value: JsonValue) -> ClaimantKind:
    match value:
        case "plan":
            return "plan"
        case "process":
            return "process"
        case _:
            raise ValueError("abort guard claimant is invalid")


def _path(value: JsonValue) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.startswith("/")
        or value != PurePosixPath(value).as_posix()
        or "//" in value
        or "/../" in value
        or "/./" in value
    ):
        raise ValueError("abort guard path is invalid")
    return value


def _hash(value: JsonValue) -> str:
    if (
        not isinstance(value, str)
        or _SHA256.fullmatch(value) is None
        or value == "0" * 64
    ):
        raise ValueError("abort guard hash is invalid")
    return value


def _authorized_guard_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == _GUARD_FILE_MODE
    )


def _guard_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
