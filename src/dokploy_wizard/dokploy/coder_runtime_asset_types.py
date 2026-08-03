"""Typed contracts shared by Coder runtime asset validation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeAlias

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import KdenseRuntimeContract

ARCHITECTURES: Final = frozenset({"amd64", "arm64"})
RUNTIME_PACKAGES: Final = frozenset(
    {"ca-certificates", "curl", "git", "wget", "btop", "python3"}
)
ARCHITECTURE_LOCKS: Final = {
    "zellij": (
        "v0.44.3",
        {
            "amd64": (
                "https://github.com/zellij-org/zellij/releases/download/v0.44.3/zellij-x86_64-unknown-linux-musl.tar.gz",
                "0f7c346788627f506c0a28296517768633cff24fc822a739f8264b640ecad751",
            ),
            "arm64": (
                "https://github.com/zellij-org/zellij/releases/download/v0.44.3/zellij-aarch64-unknown-linux-musl.tar.gz",
                "15e6534d42644d66973d136c590c49739dcfd6a1a2a0d3d917973f16c81b45fb",
            ),
        },
    ),
    "node": (
        "v22.19.0",
        {
            "amd64": (
                "https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-x64.tar.xz",
                "c0649af18e6a24f6fe5535a3e86b341dd49a8e71117c8b68bde973ef834f16f2",
            ),
            "arm64": (
                "https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-arm64.tar.xz",
                "0b2d9f564b6594222a62c82e1df2efe119dd4a4aff29644f4dd325bf360b6bcc",
            ),
        },
    ),
    "proxy_node": (
        "v24.18.0",
        {
            "amd64": (
                "https://nodejs.org/dist/v24.18.0/node-v24.18.0-linux-x64.tar.xz",
                "55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742",
            ),
            "arm64": (
                "https://nodejs.org/dist/v24.18.0/node-v24.18.0-linux-arm64.tar.xz",
                "58c9520501f6ae2b52d5b210444e24b9d0c029a58c5011b797bc1fe7105886f6",
            ),
        },
    ),
}
MUTABLE_MARKERS: Final = (
    ":latest",
    "refs/heads/",
    "--ignore-lockfile",
    "nodesource.com",
    "corepack enable",
    "curl -fsSL https://opencode.ai/install",
    "git clone --branch",
    "git clone",
    "git fetch",
    "git checkout",
    "apt-get update",
    "apt-get install",
    "/releases/latest/",
    "latest-v",
    "pnpm install",
    "npm install",
    "npm ci",
    "uv python install",
    "uv sync",
    "uv run ",
    "pkill -f",
    "opencode.ai/zen/go",
    "hermes-local-api-key",
    "sk-litellm-local",
    "openwork-client-token",
    "openwork-host-token",
    "\"@earendil-works/pi-agent-core\": \"^",
    "\"@earendil-works/pi-ai\": \"^",
    "\"@earendil-works/pi-web-ui\": \"^",
    "\"typescript\": \"^",
    "\"vite\": \"^",
    "~> ",
    " | bash",
    " | sh",
)

JsonValue: TypeAlias = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class RuntimeAssetError(ValueError):
    """Raised when an immutable runtime contract is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Validated immutable inputs used to build workspace runtime images."""

    coder_image: str
    workspace_base_image: str
    source_date_epoch: int
    release_assets: Mapping[str, str]
    lock_sha256: str
    kdense: KdenseRuntimeContract

    def derived_image_tag(self, architecture: str) -> str:
        if architecture not in ARCHITECTURES:
            raise RuntimeAssetError("runtime architecture is invalid")
        fingerprint = hashlib.sha256(f"{self.lock_sha256}:{architecture}".encode()).hexdigest()[:16]
        return f"dokploy-wizard/coder-runtime:{fingerprint}"


@dataclass(frozen=True, slots=True)
class DerivedImageInspection:
    """Immutable evidence collected after a derived runtime image build."""

    architecture: str
    tag: str
    image_id: str
    labels: Mapping[str, str]
    layers: tuple[str, ...]


def require_mapping(value: JsonValue, label: str) -> Mapping[str, JsonValue]:
    """Return a JSON object or fail closed with its runtime-contract label."""

    if not isinstance(value, dict):
        raise RuntimeAssetError(f"{label} must be an object")
    return value


def require_text(value: JsonValue, label: str) -> str:
    """Return a required JSON string or fail closed with its runtime-contract label."""

    if not isinstance(value, str) or not value:
        raise RuntimeAssetError(f"{label} is invalid")
    return value


def require_sha256(value: JsonValue, label: str) -> str:
    """Return a lowercase SHA-256 hex digest or fail closed."""

    text = require_text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RuntimeAssetError(f"{label} is invalid")
    return text


def require_digest_image(value: JsonValue, label: str) -> str:
    """Return a repository@sha256 image reference or fail closed."""

    image = require_text(value, label)
    repository, separator, digest = image.partition("@sha256:")
    if not repository or not separator:
        raise RuntimeAssetError(f"{label} must be digest pinned")
    require_sha256(digest, label)
    return image
