"""Non-secret six-template baseline normalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

REQUIRED_TEMPLATE_NAMES: Final = (
    "ubuntu-vscode",
    "ubuntu-vscode-opencode-web",
    "ubuntu-vscode-openwork",
    "ubuntu-vscode-kdense-byok",
    "ubuntu-vscode-hermes",
    "ubuntu-vscode-pi-web",
)


@dataclass(frozen=True, slots=True)
class TemplatePointer:
    name: str
    target: str
    pointer_sha256: str
    mode: str
    shape: str
    base_url: str
    credential_value_sha256: str
    independent_renderer_sha256: str


def build_template_baseline(pointers: tuple[TemplatePointer, ...]) -> list[dict[str, str | bool]]:
    """Return an exact six-template, value-free baseline in deterministic order."""
    by_name = {pointer.name: pointer for pointer in pointers}
    if tuple(sorted(by_name)) != tuple(sorted(REQUIRED_TEMPLATE_NAMES)):
        raise ValueError("baseline must contain exactly the six required Coder templates")
    return [
        {
            "name": pointer.name,
            "target": pointer.target,
            "pointer_sha256": pointer.pointer_sha256,
            "mode": pointer.mode,
            "shape": pointer.shape,
            "base_url": pointer.base_url,
            "credential_value_sha256": pointer.credential_value_sha256,
            "independent_renderer_sha256": pointer.independent_renderer_sha256,
            "legacy_exact": pointer.pointer_sha256 == pointer.independent_renderer_sha256,
        }
        for pointer in sorted(pointers, key=lambda item: item.name)
    ]


def canonical_sha256(payload: list[dict[str, str | bool]]) -> str:
    """Fingerprint a normalized baseline without recording any secret value."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
