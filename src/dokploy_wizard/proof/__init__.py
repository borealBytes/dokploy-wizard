# ruff: noqa: E501
"""Crash-safe proof tooling for the Coder/LiteLLM model-sync baseline."""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from typing import Final, Literal, NamedTuple, TypeAlias, assert_never
from urllib.parse import urlsplit

from dokploy_wizard.verification import redact_text

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_REPOSITORY_ROOT: Final = Path(__file__).parents[3]
_GUARD_FILE_MODE: Final = 0o600
_MAX_GUARD_BYTES: Final = 256 * 1024
_READ_CHUNK_BYTES: Final = 64 * 1024
_MAX_PROTECTED_ENTRIES: Final = 1_024
_MAX_PROTECTED_FILE_BYTES: Final = 16 * 1024 * 1024
_MAX_PROTECTED_TOTAL_BYTES: Final = 64 * 1024 * 1024
_MAX_PROTECTED_MANIFEST_BYTES: Final = 256 * 1024
_MAX_PROTECTED_RECEIPT_BYTES: Final = 256
_MAX_DATA_OUTPUT_BYTES: Final = 16 * 1024 * 1024
_MAX_RESULT_BYTES: Final = 256 * 1024
_RESULT_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_RESULT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
HostIdentityMode = Literal["distinct", "single_sequential"]
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
    "/single-host-lifecycle-baseline.json",
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
        "host_identity_mode",
        "host_identities_distinct",
        "legacy_workspace_managed_fingerprints_sha256",
        "litellm_image_digest",
        "proof_commit",
        "protected_artifacts_before_path",
        "protected_artifacts_before_sha256",
        "preexisting_cloudflare_sha256",
        "post_install_cloudflare_sha256",
        "schema_version",
        "shared_core_image_digests",
        "single_host_lifecycle_path",
        "single_host_lifecycle_sha256",
        "source_base_commit",
        "temporal_clean_epoch_evidence",
    }
)
C1_UNBOUND_POST_INSTALL_CLOUDFLARE_SHA256: Final = "c" * 64


class FinalizationBoundary(StrEnum):
    FINALIZE_INTENT = "finalize-intent"
    BASELINE_PUBLISHED = "baseline-published"
    HOST_A_PREFLIGHT_PUBLISHED = "host-a-preflight-published"
    HOST_B_PREFLIGHT_PUBLISHED = "host-b-preflight-published"
    SINGLE_HOST_LIFECYCLE_PUBLISHED = "single-host-lifecycle-published"
    RESULT_PUBLISHED = "result-published"
    COMPLETE_GUARD = "complete-guard"
    ROLLBACK_OUTPUT_UNLINKED = "rollback-output-unlinked"
    ROLLBACK_ENV_RESTORED = "rollback-env-restored"
    ROLLBACK_BACKUP_UNLINKED = "rollback-backup-unlinked"


BoundaryHook: TypeAlias = Callable[[FinalizationBoundary], None]
FINALIZATION_OUTPUT_BOUNDARIES: Final = {
    "baseline.json": FinalizationBoundary.BASELINE_PUBLISHED,
    "host-a-preflight.json": FinalizationBoundary.HOST_A_PREFLIGHT_PUBLISHED,
    "host-b-preflight.json": FinalizationBoundary.HOST_B_PREFLIGHT_PUBLISHED,
    "single-host-lifecycle-baseline.json": (FinalizationBoundary.SINGLE_HOST_LIFECYCLE_PUBLISHED),
}


def ignore_finalization_boundary(_boundary: FinalizationBoundary) -> None:
    """Keep boundary injection inert outside deterministic crash tests."""


@dataclass(frozen=True, slots=True)
class CaptureSchemaError(RuntimeError):
    detail: str

    def __str__(self) -> str:
        return self.detail


class AbortGuardError(RuntimeError):
    """Raised when durable GuardV3 evidence is invalid or unauthorized."""


@dataclass(frozen=True, slots=True)
class EnvPreparationError(RuntimeError):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class PreparedEnv:
    env_file: Path
    backup_path: Path
    original_sha256: str
    proof_sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class ProofNamespace:
    """Exact non-secret resource names derived from the proof environment."""

    stack_name: str
    docker: tuple[str, ...]
    dokploy: tuple[str, ...]
    cloudflare: tuple[str, ...]
    tailscale: tuple[str, ...]
    coder_templates: tuple[str, ...]

    def to_dict(self) -> dict[str, list[str] | str]:
        """Return the remote probe contract without retaining raw env values."""
        return {
            "cloudflare": list(self.cloudflare),
            "coder": list(self.coder_templates),
            "docker": list(self.docker),
            "dokploy": list(self.dokploy),
            "stack_name": self.stack_name,
            "tailscale": list(self.tailscale),
        }


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
    host_identity_mode: HostIdentityMode
    post_install_cloudflare_sha256: str
    output_sha256: Mapping[str, str]
    result_body: Mapping[str, JsonValue]

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "artifact_dir": self.artifact_dir,
            "env_receipt": self.env_receipt.to_payload(),
            "guard_id": self.guard_id,
            "guard_path": self.guard_path,
            "host_identity_mode": self.host_identity_mode,
            "post_install_cloudflare_sha256": self.post_install_cloudflare_sha256,
            "kind": "task-1-baseline-finalization",
            "output_sha256": dict(self.output_sha256),
            "required_terminal": {
                "claimant_kind": "plan",
                "phase": "complete",
                "state": "armed",
            },
            "result_body": dict(self.result_body),
            "result_path": self.result_path,
            "schema_version": 2,
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


def read_bounded_regular_bytes(
    path: Path, max_bytes: int, required_mode: int | None
) -> tuple[bytes, int]:
    before = os.lstat(path)
    file_mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or not 1 <= file_mode <= 0o777
        or before.st_size > max_bytes
        or (required_mode is not None and file_mode != required_mode)
    ):
        raise ValueError("proof env or backup is not an authorized bounded regular file")
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
    )
    try:
        opened = os.fstat(descriptor)
        if _guard_metadata(before) != _guard_metadata(opened):
            raise ValueError("proof env or backup changed during inspection")
        limit = opened.st_size + 1
        content = bytearray()
        while len(content) < limit:
            remaining = min(_READ_CHUNK_BYTES, limit - len(content))
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            if len(chunk) > remaining:
                raise ValueError("proof env or backup exceeded its inspection bound")
            content.extend(chunk)
        after = os.fstat(descriptor)
        pathname = os.lstat(path)
        if (
            len(content) != opened.st_size
            or _guard_metadata(opened) != _guard_metadata(after)
            or _guard_metadata(opened) != _guard_metadata(pathname)
        ):
            raise ValueError("proof env or backup changed during inspection")
        return bytes(content), file_mode
    finally:
        os.close(descriptor)


def open_protected_directory(
    component: str, parent_descriptor: int
) -> tuple[int, tuple[int, int, int]]:
    descriptor = os.open(
        component,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def _open_protected_parent(
    root_descriptor: int, parts: tuple[str, ...]
) -> tuple[int, tuple[tuple[int, int, int], ...]]:
    parent = os.dup(root_descriptor)
    identities: list[tuple[int, int, int]] = []
    try:
        for component in parts:
            previous = parent
            parent, identity = open_protected_directory(component, previous)
            os.close(previous)
            identities.append(identity)
    except BaseException:
        os.close(parent)
        raise
    return parent, tuple(identities)


def _revalidate_protected_parent(
    root_descriptor: int,
    parts: tuple[str, ...],
    expected_identities: tuple[tuple[int, int, int], ...],
) -> int:
    parent = os.dup(root_descriptor)
    try:
        for component, expected in zip(parts, expected_identities, strict=True):
            previous = parent
            parent, actual = open_protected_directory(component, previous)
            os.close(previous)
            if actual != expected:
                raise AbortGuardError("protected artifact directory identity changed")
    except BaseException:
        os.close(parent)
        raise
    return parent


def _hash_protected_file(
    descriptor: int, aggregate_bytes: int
) -> tuple[os.stat_result, os.stat_result, int, str]:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size > _MAX_PROTECTED_FILE_BYTES
        or aggregate_bytes + before.st_size > _MAX_PROTECTED_TOTAL_BYTES
    ):
        raise AbortGuardError("protected artifact is not a bounded regular file")
    digest = hashlib.sha256()
    file_bytes = 0
    total_bytes = aggregate_bytes
    while True:
        request = min(
            _READ_CHUNK_BYTES,
            _MAX_PROTECTED_FILE_BYTES - file_bytes + 1,
            _MAX_PROTECTED_TOTAL_BYTES - total_bytes + 1,
        )
        chunk = os.read(descriptor, request)
        if not chunk:
            break
        file_bytes += len(chunk)
        total_bytes += len(chunk)
        if file_bytes > _MAX_PROTECTED_FILE_BYTES or total_bytes > _MAX_PROTECTED_TOTAL_BYTES:
            raise AbortGuardError("protected artifact grew beyond its bound")
        digest.update(chunk)
    after = os.fstat(descriptor)
    if file_bytes != before.st_size:
        raise AbortGuardError("protected artifact ended before its recorded size")
    return before, after, total_bytes, digest.hexdigest()


def verify_protected_artifacts(
    repository_root: Path, entries: tuple[ProtectedManifestEntry, ...]
) -> None:
    if len(entries) > _MAX_PROTECTED_ENTRIES:
        raise AbortGuardError("protected artifact entry limit exceeded")
    root_descriptor = os.open(
        repository_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    aggregate_bytes = 0
    try:
        for entry in entries:
            parent, identities = _open_protected_parent(root_descriptor, entry.path.parts[:-1])
            try:
                descriptor = os.open(
                    entry.path.parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=parent,
                )
                try:
                    before, after, aggregate_bytes, fingerprint = _hash_protected_file(
                        descriptor, aggregate_bytes
                    )
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent)
            revalidated = _revalidate_protected_parent(
                root_descriptor, entry.path.parts[:-1], identities
            )
            try:
                named = os.stat(
                    entry.path.parts[-1],
                    dir_fd=revalidated,
                    follow_symlinks=False,
                )
            finally:
                os.close(revalidated)
            if (
                _guard_metadata(before) != _guard_metadata(after)
                or _guard_metadata(after) != _guard_metadata(named)
                or fingerprint != entry.sha256
            ):
                raise AbortGuardError("protected artifact verification failed")
    finally:
        os.close(root_descriptor)


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
        "host_identity_mode",
        "post_install_cloudflare_sha256",
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
        or value["schema_version"] != 2
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
        _host_identity_mode(value["host_identity_mode"]),
        _hash(value["post_install_cloudflare_sha256"]),
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
    match attestation.host_identity_mode:
        case "distinct":
            expected_outputs = {
                "baseline.json",
                "host-a-preflight.json",
                "host-b-preflight.json",
                "protected-artifacts-before.txt",
            }
        case "single_sequential":
            expected_outputs = {
                "baseline.json",
                "host-a-preflight.json",
                "protected-artifacts-before.txt",
                "single-host-lifecycle-baseline.json",
            }
        case unexpected:
            assert_never(unexpected)
    if set(attestation.output_sha256) != expected_outputs:
        raise ValueError("abort guard attestation outputs are invalid")
    outputs = {key: _hash(value) for key, value in attestation.output_sha256.items()}
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
        "host_identity_mode": attestation.host_identity_mode,
        "protected_artifacts_before_path": f"{artifact_dir}/protected-artifacts-before.txt",
        "protected_artifacts_before_sha256": outputs["protected-artifacts-before.txt"],
        "post_install_cloudflare_sha256": _hash(attestation.post_install_cloudflare_sha256),
    }
    match attestation.host_identity_mode:
        case "distinct":
            expected_bindings.update(
                {
                    "host_b_preflight_sha256": outputs["host-b-preflight.json"],
                    "single_host_lifecycle_path": None,
                    "single_host_lifecycle_sha256": None,
                }
            )
        case "single_sequential":
            expected_bindings.update(
                {
                    "host_b_preflight_sha256": None,
                    "single_host_lifecycle_path": (
                        f"{artifact_dir}/single-host-lifecycle-baseline.json"
                    ),
                    "single_host_lifecycle_sha256": outputs["single-host-lifecycle-baseline.json"],
                }
            )
        case unexpected:
            assert_never(unexpected)
    if result_path != f"{artifact_dir}/result.json" or any(
        body[key] != value for key, value in expected_bindings.items()
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
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 3:
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
        or receipt_identity(guard.attestation.env_receipt) != receipt_identity(guard.env_receipt)
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
        actual = process_start_time_ticks(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
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
            (
                "ready"
                | "claimed"
                | "env_intent"
                | "proof_active"
                | "finalize_intent"
                | "rollback"
                | "complete"
            ) as phase
        ):
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


def _host_identity_mode(value: JsonValue) -> HostIdentityMode:
    match value:
        case "distinct":
            return "distinct"
        case "single_sequential":
            return "single_sequential"
        case _:
            raise ValueError("host identity mode is invalid")


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
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None or value == "0" * 64:
        raise ValueError("abort guard hash is invalid")
    return value


def _authorized_guard_metadata(metadata: os.stat_result) -> bool:
    return stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == _GUARD_FILE_MODE


def _guard_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class GuardClaim:
    token: str
    pid: int
    start_time_ticks: str


@dataclass(frozen=True, slots=True)
class ProofRecoveryPaths:
    env_file: Path
    backup_path: Path
    guard_path: Path
    artifact_dir: Path
    output: Path
    repository_root: Path


def require_active_repository_root(wrapper: Path, paths: ProofRecoveryPaths) -> Path:
    """Bind every local baseline path to the canonical running checkout."""
    running_root = _require_canonical_existing(
        _ACTIVE_REPOSITORY_ROOT,
        "running repository root",
    )
    root = _require_canonical_existing(paths.repository_root, "active root")
    if root != running_root:
        raise RuntimeError("active root does not match the running repository checkout")
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise RuntimeError("active root must be an ordinary directory")

    expected_wrapper = root / "bin" / "dokploy-wizard-remote"
    if wrapper != expected_wrapper:
        raise RuntimeError("wrapper must be the canonical active-root remote wrapper")
    resolved_wrapper = _require_canonical_existing(wrapper, "wrapper")
    if resolved_wrapper != expected_wrapper or not stat.S_ISREG(os.lstat(wrapper).st_mode):
        raise RuntimeError("wrapper must be an ordinary file under the active root")

    expected_env = root / ".install-min.env"
    if paths.env_file != expected_env:
        raise RuntimeError("env file must be the canonical active-root .install-min.env")
    resolved_env = _require_canonical_existing(paths.env_file, "env file")
    env_metadata = os.lstat(resolved_env)
    if not stat.S_ISREG(env_metadata.st_mode) or stat.S_IMODE(env_metadata.st_mode) != 0o600:
        raise RuntimeError("active-root .install-min.env must be an ordinary mode-0600 file")

    artifact_dir = _require_canonical_existing(paths.artifact_dir, "artifact directory")
    if not stat.S_ISDIR(os.lstat(artifact_dir).st_mode):
        raise RuntimeError("artifact directory must be an ordinary directory")
    try:
        artifact_relative = artifact_dir.relative_to(root)
    except ValueError as error:
        raise RuntimeError("artifact directory must be beneath the active root") from error
    if artifact_relative == Path("."):
        raise RuntimeError("artifact directory must be nested beneath the active root")

    for requested, name in (
        (paths.guard_path, "abort-guard.json"),
        (paths.output, "result.json"),
    ):
        expected = artifact_dir / name
        if requested != expected:
            raise RuntimeError(f"{name} must be the canonical artifact path")
        if os.path.lexists(requested):
            resolved = _require_canonical_existing(requested, name)
            if resolved != expected or not stat.S_ISREG(os.lstat(requested).st_mode):
                raise RuntimeError(f"{name} must be an ordinary artifact file")

    backup = paths.backup_path
    if not backup.is_absolute() or ".." in backup.parts:
        raise RuntimeError("external backup path must be absolute and traversal-free")
    try:
        backup.relative_to(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("external backup path must remain outside the active root")
    backup_ancestor = backup.parent
    while not os.path.lexists(backup_ancestor):
        backup_ancestor = backup_ancestor.parent
    _require_canonical_existing(backup_ancestor, "external backup ancestor")
    if os.path.lexists(backup):
        resolved_backup = _require_canonical_existing(backup, "external backup")
        backup_metadata = os.lstat(resolved_backup)
        if not stat.S_ISREG(backup_metadata.st_mode):
            raise RuntimeError("external backup must be an ordinary file")
    return root


def _require_canonical_existing(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"{label} must exist") from error
    if not path.is_absolute() or path != resolved:
        raise RuntimeError(f"{label} must be an absolute canonical path without symlinks")
    return resolved


@dataclass(frozen=True, slots=True)
class ProofRecovery:
    paths: ProofRecoveryPaths
    claim: GuardClaim | None
    terminal: bool
    resumable: bool


def build_model_sync_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model-sync-live-proof")
    commands = parser.add_subparsers(dest="command", required=True)
    finalize = commands.add_parser("atomic-finalize")
    finalize.add_argument("--temp", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    status = commands.add_parser("abort-status")
    status.add_argument("--guard", type=Path, required=True)
    status.add_argument("--output", type=Path, required=True)
    baseline = commands.add_parser("baseline-host-a")
    baseline.add_argument("--active-root", type=Path, required=True)
    baseline.add_argument("--wrapper", type=Path, required=True)
    baseline.add_argument("--env-file", type=Path, required=True)
    baseline.add_argument("--external-backup", type=Path, required=True)
    baseline.add_argument("--abort-guard", type=Path, required=True)
    baseline.add_argument("--host-env", required=True)
    baseline.add_argument("--password-env", required=True)
    baseline.add_argument("--host-b-env", required=True)
    baseline.add_argument("--host-b-password-env", required=True)
    baseline.add_argument("--single-host-sequential", action="store_true")
    baseline.add_argument("--source-base-commit", required=True)
    baseline.add_argument("--proof-commit", required=True)
    baseline.add_argument("--artifact-dir", type=Path, required=True)
    baseline.add_argument("--output", type=Path, required=True)
    return parser


def self_start_time_ticks() -> str:
    return process_start_time_ticks(Path("/proc/self/stat").read_text(encoding="utf-8"))


def abort_status_payload(status: AbortGuard) -> dict[str, JsonScalar]:
    if status.phase != "complete" or status.claimant_kind != "plan" or status.attestation is None:
        raise AbortGuardError("abort guard has unresolved recovery state")
    temporal = status.attestation.result_body["temporal_clean_epoch_evidence"]
    if not isinstance(temporal, bool):
        raise AbortGuardError("abort guard host provenance is invalid")
    return {
        "state": status.state,
        "phase": status.phase,
        "claimant_kind": status.claimant_kind,
        "host_identity_mode": status.attestation.host_identity_mode,
        "host_identities_distinct": status.attestation.host_identity_mode == "distinct",
        "pid": status.pid,
        "start_time_ticks": status.start_time_ticks,
        "temporal_clean_epoch_evidence": temporal,
    }


@dataclass(frozen=True, slots=True)
class BaselineResultEvidence:
    source_base_commit: str
    proof_commit: str
    host_identity_mode: HostIdentityMode
    images: Mapping[str, str]
    env_receipt: EnvReceipt
    guard_path: Path
    artifact_dir: Path
    abort_guard_sha256: str
    host_a_preflight_sha256: str
    host_b_preflight_sha256: str | None
    single_host_lifecycle_sha256: str | None
    baseline_sha256: str
    protected_artifacts_before_sha256: str
    coder_secret_inventory_sha256: str
    legacy_workspace_managed_fingerprints_sha256: str
    preexisting_cloudflare_sha256: str
    post_install_cloudflare_sha256: str


def run_bounded_process(
    command: Sequence[str],
    *,
    stdin: bytes,
    output_limit: int,
    timeout_seconds: float,
    label: str,
) -> bytes:
    if output_limit < 1 or timeout_seconds <= 0 or not label:
        raise ValueError("bounded process limits are invalid")
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr_size = 0
    pending = memoryview(stdin)
    deadline = time.monotonic() + timeout_seconds
    failure: str | None = None
    returncode: int | None = None
    for stream, role in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, role)
    os.set_blocking(process.stdin.fileno(), False)
    if pending:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    try:
        while selector.get_map() and failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timed out"
                break
            for key, _mask in selector.select(remaining):
                if key.data == "stdin":
                    try:
                        pending = pending[os.write(key.fd, pending) :]
                    except BrokenPipeError:
                        pending = pending[len(pending) :]
                    if not pending:
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    (process.stdout if key.data == "stdout" else process.stderr).close()
                    continue
                if key.data == "stdout":
                    stdout.extend(chunk)
                    size = len(stdout)
                else:
                    stderr_size += len(chunk)
                    size = stderr_size
                if size > output_limit:
                    failure = "exceeded output limit"
                    break
        if failure is None:
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        failure = "failed" if time.monotonic() < deadline else "timed out"
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    if failure is not None:
        raise RuntimeError(f"{label} {failure}")
    if returncode != 0:
        raise RuntimeError(f"{label} failed")
    return bytes(stdout)


def atomic_finalize(*, temp: Path, output: Path) -> None:
    if temp.parent.resolve() != output.parent.resolve() or temp.is_symlink() or not temp.is_file():
        raise ValueError("atomic finalization requires a regular sibling temporary file")
    try:
        if output.exists():
            if (
                output.is_symlink()
                or not output.is_file()
                or output.stat().st_mode & 0o777 != 0o600
                or not filecmp.cmp(temp, output, shallow=False)
            ):
                raise ValueError("existing output does not match finalized bytes")
            temp.unlink()
            return
        descriptor = os.open(temp, os.O_RDONLY)
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, output)
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def build_result(values: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    if frozenset(values) != REQUIRED_RESULT_KEYS:
        raise ValueError("Task 1 result keys do not match the proof contract")
    _require_result_hashes(values)
    _require_result_digests(values)
    _require_capture_values(values)
    return dict(values)


def validate_attestation(attestation: BaselineAttestation) -> None:
    try:
        build_result({**attestation.result_body, "abort_guard_sha256": "f" * 64})
        validate_baseline_attestation_bindings(attestation)
    except ValueError as error:
        raise ValueError("abort guard attestation result body is invalid") from error


def derive_result_from_attestation(attestation: BaselineAttestation) -> dict[str, JsonValue]:
    validate_attestation(attestation)
    return build_result(
        {
            **attestation.result_body,
            "abort_guard_sha256": abort_guard_sha256(attestation),
        }
    )


def result_bytes_from_attestation(attestation: BaselineAttestation) -> bytes:
    return canonical_json_bytes(derive_result_from_attestation(attestation)) + b"\n"


def verify_result_bytes(attestation: BaselineAttestation, value: bytes) -> None:
    if value != result_bytes_from_attestation(attestation):
        raise ValueError("result bytes do not match abort guard attestation")


def baseline_result_values(evidence: BaselineResultEvidence) -> dict[str, JsonValue]:
    return {
        "schema_version": 2,
        "source_base_commit": evidence.source_base_commit,
        "proof_commit": evidence.proof_commit,
        "host_identity_mode": evidence.host_identity_mode,
        "coder_image_digest": evidence.images["coder"],
        "litellm_image_digest": evidence.images["litellm"],
        "shared_core_image_digests": {
            "pgvector": evidence.images["pgvector"],
            "redis": evidence.images["redis"],
            "postfix": evidence.images["postfix"],
            "litellm": evidence.images["litellm"],
        },
        "env_original_sha256": evidence.env_receipt.original_sha256,
        "env_proof_sha256": evidence.env_receipt.proof_sha256,
        "env_mode": evidence.env_receipt.mode,
        "external_backup_path": evidence.env_receipt.backup_path,
        "abort_guard_path": str(evidence.guard_path.resolve()),
        "abort_guard_sha256": evidence.abort_guard_sha256,
        "host_a_preflight_sha256": evidence.host_a_preflight_sha256,
        "host_b_preflight_sha256": evidence.host_b_preflight_sha256,
        "host_identities_distinct": evidence.host_identity_mode == "distinct",
        "host_architectures_equal": (True if evidence.host_identity_mode == "distinct" else None),
        "single_host_lifecycle_path": (
            str((evidence.artifact_dir / "single-host-lifecycle-baseline.json").resolve())
            if evidence.single_host_lifecycle_sha256 is not None
            else None
        ),
        "single_host_lifecycle_sha256": evidence.single_host_lifecycle_sha256,
        "temporal_clean_epoch_evidence": False,
        "baseline_sha256": evidence.baseline_sha256,
        "protected_artifacts_before_path": str(
            (evidence.artifact_dir / "protected-artifacts-before.txt").resolve()
        ),
        "protected_artifacts_before_sha256": (evidence.protected_artifacts_before_sha256),
        "coder_secret_inventory_sha256": evidence.coder_secret_inventory_sha256,
        "legacy_workspace_managed_fingerprints_sha256": (
            evidence.legacy_workspace_managed_fingerprints_sha256
        ),
        "preexisting_cloudflare_sha256": evidence.preexisting_cloudflare_sha256,
        "post_install_cloudflare_sha256": evidence.post_install_cloudflare_sha256,
    }


def baseline_output_hashes(evidence: BaselineResultEvidence) -> dict[str, str]:
    outputs = {
        "baseline.json": evidence.baseline_sha256,
        "host-a-preflight.json": evidence.host_a_preflight_sha256,
        "protected-artifacts-before.txt": evidence.protected_artifacts_before_sha256,
    }
    if evidence.host_b_preflight_sha256 is not None:
        outputs["host-b-preflight.json"] = evidence.host_b_preflight_sha256
    if evidence.single_host_lifecycle_sha256 is not None:
        outputs["single-host-lifecycle-baseline.json"] = evidence.single_host_lifecycle_sha256
    return outputs


def build_baseline_attestation(
    evidence: BaselineResultEvidence,
    *,
    guard_id: str,
    result_path: Path,
) -> BaselineAttestation:
    body = build_result(baseline_result_values(evidence))
    return BaselineAttestation(
        guard_id,
        str(evidence.guard_path.resolve()),
        str(evidence.artifact_dir.resolve()),
        str(result_path.resolve()),
        evidence.env_receipt,
        evidence.host_identity_mode,
        evidence.post_install_cloudflare_sha256,
        baseline_output_hashes(evidence),
        {key: value for key, value in body.items() if key != "abort_guard_sha256"},
    )


def require_generated_bounds(payloads: Mapping[str, bytes], result: bytes) -> None:
    if any(len(content) > _MAX_DATA_OUTPUT_BYTES for content in payloads.values()):
        raise AbortGuardError("generated data output exceeds its bound")
    if len(result) > _MAX_RESULT_BYTES:
        raise AbortGuardError("generated result output exceeds its bound")


def read_proof_bytes(path: Path, max_bytes: int, mode: int) -> bytes:
    try:
        return read_bounded_regular_bytes(path, max_bytes, mode)[0]
    except (OSError, ValueError) as error:
        raise AbortGuardError("proof file is not a stable bounded regular file") from error


def protected_bytes(paths: ProofRecoveryPaths) -> bytes:
    manifest_path = paths.artifact_dir / "protected-artifacts-before.txt"
    receipt_path = paths.artifact_dir / "protected-artifacts-before.sha256"
    try:
        manifest = read_bounded_regular_bytes(
            manifest_path,
            _MAX_PROTECTED_MANIFEST_BYTES,
            0o600,
        )[0]
        entries = validate_protected_manifest_bytes(manifest)
        verify_protected_artifacts(paths.repository_root, entries)
        receipt = read_bounded_regular_bytes(
            receipt_path,
            _MAX_PROTECTED_RECEIPT_BYTES,
            0o600,
        )[0]
    except (CaptureSchemaError, OSError, ValueError) as error:
        raise AbortGuardError("protected manifest contract is invalid") from error
    fingerprint = hashlib.sha256(manifest).hexdigest()
    expected = f"{fingerprint}  protected-artifacts-before.txt\n".encode()
    if receipt != expected:
        raise AbortGuardError("pre-existing protected manifest receipt is invalid")
    return manifest


def output_paths(
    paths: ProofRecoveryPaths,
    names: Iterable[str] | None = None,
) -> dict[str, Path]:
    available = {
        "baseline.json": paths.artifact_dir / "baseline.json",
        "host-a-preflight.json": paths.artifact_dir / "host-a-preflight.json",
        "host-b-preflight.json": paths.artifact_dir / "host-b-preflight.json",
        "single-host-lifecycle-baseline.json": (
            paths.artifact_dir / "single-host-lifecycle-baseline.json"
        ),
    }
    if names is None:
        return available
    requested = set(names) - {"protected-artifacts-before.txt"}
    if not requested <= set(available):
        raise AbortGuardError("attestation contains an unsupported output name")
    return {name: available[name] for name in requested}


def assert_no_generated_outputs(paths: ProofRecoveryPaths) -> None:
    if any(os.path.lexists(path) for path in (*output_paths(paths).values(), paths.output)):
        raise AbortGuardError("generated output exists without an authorizing attestation")


def verify_attestation(
    paths: ProofRecoveryPaths,
    guard: AbortGuard,
    *,
    require_result: bool,
) -> None:
    attestation = guard.attestation
    receipt = guard.env_receipt
    if (
        attestation is None
        or receipt is None
        or guard.phase not in {"finalize_intent", "complete"}
        or attestation.guard_path != str(paths.guard_path.resolve())
        or attestation.artifact_dir != str(paths.artifact_dir.resolve())
        or attestation.result_path != str(paths.output.resolve())
        or receipt.env_path != str(paths.env_file.resolve())
        or receipt.backup_path != str(paths.backup_path.resolve())
    ):
        raise AbortGuardError("attestation paths do not bind the current recovery paths")
    proof_mode = receipt.mode if receipt.original_sha256 == receipt.proof_sha256 else 0o600
    if (
        hashlib.sha256(read_proof_bytes(paths.env_file, 256 * 1024, proof_mode)).hexdigest()
        != receipt.proof_sha256
        or hashlib.sha256(read_proof_bytes(paths.backup_path, 256 * 1024, 0o600)).hexdigest()
        != receipt.original_sha256
    ):
        raise AbortGuardError("proof env or backup drifted from its receipt")
    manifest = protected_bytes(paths)
    if (
        attestation.output_sha256["protected-artifacts-before.txt"]
        != hashlib.sha256(manifest).hexdigest()
    ):
        raise AbortGuardError("protected manifest drifted from attestation")
    for name, path in output_paths(paths, attestation.output_sha256).items():
        content = read_proof_bytes(path, _MAX_DATA_OUTPUT_BYTES, 0o600)
        if hashlib.sha256(content).hexdigest() != attestation.output_sha256[name]:
            raise AbortGuardError("attested output drifted")
    expected_result = result_bytes_from_attestation(attestation)
    require_generated_bounds({}, expected_result)
    if require_result or os.path.lexists(paths.output):
        try:
            result = read_proof_bytes(paths.output, _MAX_RESULT_BYTES, 0o600)
        except AbortGuardError as error:
            raise AbortGuardError("result bytes do not match attestation") from error
        if result != expected_result:
            raise AbortGuardError("result bytes do not match attestation")


def attestation_is_resumable(paths: ProofRecoveryPaths, guard: AbortGuard) -> bool:
    try:
        verify_attestation(paths, guard, require_result=False)
    except (AbortGuardError, CaptureSchemaError, EnvPreparationError, OSError):
        return False
    return True


def _require_result_hashes(values: Mapping[str, JsonValue]) -> None:
    keys = (
        "env_original_sha256",
        "env_proof_sha256",
        "abort_guard_sha256",
        "host_a_preflight_sha256",
        "baseline_sha256",
        "protected_artifacts_before_sha256",
        "coder_secret_inventory_sha256",
        "legacy_workspace_managed_fingerprints_sha256",
        "preexisting_cloudflare_sha256",
        "post_install_cloudflare_sha256",
    )
    for key in keys:
        value = values[key]
        if not isinstance(value, str) or not _SHA256.fullmatch(value) or value == "0" * 64:
            raise ValueError("all capture hashes must be non-zero SHA-256 values")
    mode = _host_identity_mode(values["host_identity_mode"])
    match mode:
        case "distinct":
            required = values["host_b_preflight_sha256"]
            if values["single_host_lifecycle_sha256"] is not None:
                raise ValueError("distinct-host result cannot bind a single-host lifecycle")
        case "single_sequential":
            required = values["single_host_lifecycle_sha256"]
            if values["host_b_preflight_sha256"] is not None:
                raise ValueError("single-host result cannot bind a Host B preflight")
        case unexpected:
            assert_never(unexpected)
    if not isinstance(required, str) or not _SHA256.fullmatch(required) or required == "0" * 64:
        raise ValueError("mode-specific capture hash must be a non-zero SHA-256 value")


def _require_result_digests(values: Mapping[str, JsonValue]) -> None:
    shared = values["shared_core_image_digests"]
    if not isinstance(shared, dict) or set(shared) != {
        "pgvector",
        "redis",
        "postfix",
        "litellm",
    }:
        raise ValueError("shared core image digest manifest is invalid")
    image_values = [
        values["coder_image_digest"],
        values["litellm_image_digest"],
        *shared.values(),
    ]
    if any(
        not isinstance(value, str) or not _RESULT_DIGEST.fullmatch(value) for value in image_values
    ):
        raise ValueError("all captured images must use repository@sha256 digests")
    if shared["litellm"] != values["litellm_image_digest"]:
        raise ValueError("LiteLLM image observations must agree across result planes")


def _require_capture_values(values: Mapping[str, JsonValue]) -> None:
    commits = (values["source_base_commit"], values["proof_commit"])
    if any(not isinstance(value, str) or not _RESULT_COMMIT.fullmatch(value) for value in commits):
        raise ValueError("proof commits must be exact SHA-1 values")
    schema_version = values["schema_version"]
    env_mode = values["env_mode"]
    if (
        isinstance(schema_version, bool)
        or schema_version != 2
        or isinstance(env_mode, bool)
        or not isinstance(env_mode, int)
        or not 1 <= env_mode <= 0o777
    ):
        raise ValueError("result schema version and proof env mode are invalid")
    mode = _host_identity_mode(values["host_identity_mode"])
    match mode:
        case "distinct":
            mode_fields_valid = (
                values["host_identities_distinct"] is True
                and values["host_architectures_equal"] is True
                and values["temporal_clean_epoch_evidence"] is False
                and values["single_host_lifecycle_path"] is None
            )
        case "single_sequential":
            mode_fields_valid = (
                values["host_identities_distinct"] is False
                and values["host_architectures_equal"] is None
                and values["temporal_clean_epoch_evidence"] is False
                and isinstance(values["single_host_lifecycle_path"], str)
                and values["single_host_lifecycle_path"] != ""
            )
        case unexpected:
            assert_never(unexpected)
    if not mode_fields_valid:
        raise ValueError("result host identity provenance is inconsistent")
    for key in (
        "external_backup_path",
        "abort_guard_path",
        "protected_artifacts_before_path",
    ):
        if not isinstance(values[key], str) or values[key] == "":
            raise ValueError(f"{key} must be a non-empty protected path")
