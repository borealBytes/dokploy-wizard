"""Immutable runtime image manifest resolved into desired and applied state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

_LOCK_PATH: Final = (
    Path(__file__).resolve().parents[1] / "dokploy" / "shared-core-images.lock.json"
)
_RUNTIME_LOCK_PATH: Final = Path(__file__).resolve().parents[1] / "runtime-manifest.lock.json"
_LOCK_NAMES: Final = frozenset({"pgvector", "redis", "postfix", "litellm"})
_OVERRIDES: Final = {
    "pgvector": "SHARED_POSTGRES_IMAGE",
    "redis": "SHARED_REDIS_IMAGE",
    "postfix": "SHARED_POSTFIX_IMAGE",
    "litellm": "LITELLM_IMAGE",
    "coder": "CODER_IMAGE",
}


class RuntimeImageError(ValueError):
    """Raised when an immutable image manifest or override is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeImages:
    """Exact images consumed by Shared Core and Coder rendering."""

    pgvector: str
    redis: str
    postfix: str
    litellm: str
    coder: str
    workspace_amd64: str | None = None
    workspace_arm64: str | None = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2}:
            raise RuntimeImageError("Runtime image manifest schema is unsupported.")
        if self.schema_version == 1 and (
            self.workspace_amd64 is not None or self.workspace_arm64 is not None
        ):
            raise RuntimeImageError("Legacy runtime images cannot contain workspace image IDs.")
        for name in _OVERRIDES:
            _require_digest_image(getattr(self, name), name)
        if (self.workspace_amd64 is None) != (self.workspace_arm64 is None):
            raise RuntimeImageError("Workspace runtime image IDs must be complete.")
        if self.workspace_amd64 is not None and self.workspace_arm64 is not None:
            _require_image_id(self.workspace_amd64, "workspace amd64")
            _require_image_id(self.workspace_arm64, "workspace arm64")

    def to_dict(self) -> dict[str, str | int | None]:
        payload: dict[str, str | int | None] = {
            "coder": self.coder,
            "litellm": self.litellm,
            "pgvector": self.pgvector,
            "postfix": self.postfix,
            "redis": self.redis,
            "schema_version": self.schema_version,
        }
        if self.schema_version == 2:
            payload["workspace_amd64"] = self.workspace_amd64
            payload["workspace_arm64"] = self.workspace_arm64
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, str | int | None]) -> RuntimeImages:
        version = payload.get("schema_version")
        if type(version) is not int or version not in {1, 2}:
            raise RuntimeImageError("Runtime image manifest version is invalid.")
        expected = set(_OVERRIDES) | {"schema_version"}
        if version == 2:
            expected |= {"workspace_amd64", "workspace_arm64"}
        if set(payload) != expected:
            raise RuntimeImageError("Runtime image manifest keys are invalid.")
        values = {name: payload[name] for name in _OVERRIDES}
        if not all(isinstance(value, str) for value in values.values()):
            raise RuntimeImageError("Runtime image manifest values are invalid.")
        workspace_amd64 = payload.get("workspace_amd64")
        workspace_arm64 = payload.get("workspace_arm64")
        if workspace_amd64 is not None and not isinstance(workspace_amd64, str):
            raise RuntimeImageError("Workspace runtime image IDs are invalid.")
        if workspace_arm64 is not None and not isinstance(workspace_arm64, str):
            raise RuntimeImageError("Workspace runtime image IDs are invalid.")
        return cls(
            pgvector=str(values["pgvector"]),
            redis=str(values["redis"]),
            postfix=str(values["postfix"]),
            litellm=str(values["litellm"]),
            coder=str(values["coder"]),
            workspace_amd64=workspace_amd64,
            workspace_arm64=workspace_arm64,
            schema_version=version,
        )


def resolve_runtime_images(values: Mapping[str, str]) -> RuntimeImages:
    """Resolve digest-only overrides over the checked-in immutable lock."""

    if values.get("LITELLM_IMAGE_TAG", "").strip() or values.get(
        "LITELLM_VERSION", ""
    ).strip():
        raise RuntimeImageError("LiteLLM tag/version overrides are forbidden; use a digest.")
    locked = _load_shared_core_lock()
    coder_image = _load_coder_lock()
    coder_override = values.get("CODER_IMAGE", "").strip()
    if coder_override and coder_override != coder_image:
        raise RuntimeImageError(
            "Coder image override must equal the accepted digest-pinned image."
        )
    resolved = {
        name: values.get(env_name, "").strip()
        or (coder_image if name == "coder" else locked[name])
        for name, env_name in _OVERRIDES.items()
    }
    return RuntimeImages(
        pgvector=resolved["pgvector"],
        redis=resolved["redis"],
        postfix=resolved["postfix"],
        litellm=resolved["litellm"],
        coder=resolved["coder"],
    )


def _load_shared_core_lock() -> dict[str, str]:
    try:
        payload = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeImageError("Shared Core image lock is unreadable.") from error
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "images"}:
        raise RuntimeImageError("Shared Core image lock shape is invalid.")
    if payload["schema_version"] != 1 or not isinstance(payload["images"], dict):
        raise RuntimeImageError("Shared Core image lock schema is invalid.")
    images = payload["images"]
    if frozenset(images) != _LOCK_NAMES or not all(
        isinstance(value, str) for value in images.values()
    ):
        raise RuntimeImageError("Shared Core image lock entries are invalid.")
    normalized = dict(images)
    for name, value in normalized.items():
        _require_digest_image(value, name)
    return normalized


def _load_coder_lock() -> str:
    try:
        payload = json.loads(_RUNTIME_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeImageError("Common runtime image lock is unreadable.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("images"), dict):
        raise RuntimeImageError("Common runtime image lock schema is invalid.")
    version = payload.get("schema_version")
    if version == 1 and set(payload) != {"schema_version", "images"}:
        raise RuntimeImageError("Common runtime image lock schema is invalid.")
    if version in {2, 3} and set(payload) != {"schema_version", "images", "workspace_runtime"}:
        raise RuntimeImageError("Common runtime image lock schema is invalid.")
    if version not in {1, 2, 3} or set(payload["images"]) != {"coder"}:
        raise RuntimeImageError("Common runtime image lock schema is invalid.")
    if not isinstance(payload["images"]["coder"], str):
        raise RuntimeImageError("Common runtime image lock schema is invalid.")
    coder = payload["images"]["coder"]
    _require_digest_image(coder, "coder")
    return coder


def _require_digest_image(value: str, name: str) -> None:
    repository, separator, digest = value.partition("@sha256:")
    if (
        repository == ""
        or separator == ""
        or "@" in repository
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RuntimeImageError(f"{name} image must be repository@sha256 digest-pinned.")


def _require_image_id(value: str, name: str) -> None:
    digest = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise RuntimeImageError(f"{name} image ID must be sha256-pinned.")
