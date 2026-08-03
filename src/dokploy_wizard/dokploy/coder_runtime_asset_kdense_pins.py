"""Immutable public K-Dense source and toolchain pins for Task 7."""

from __future__ import annotations

from typing import Final

KDENSE_COMMIT: Final = "a2502d2fd0594db38f20ef8ea91e71a24a4c0e8d"
KDENSE_TREE: Final = "d7a10cd0dd0041a63754ed0f2a4ebe323648d46f"
KDENSE_ARCHIVE: Final = "a161dfdfd908f4eecfb53c0976ba6bec71967df56a55935c5fb9c89bc48787d1"
SOURCE_PATHS: Final = frozenset(
    {
        "server/package-lock.json",
        "web/package-lock.json",
        "server/src/helpers/pyproject.toml",
        "server/src/prep.ts",
        "server/src/sandbox-seed.ts",
        "start.mjs",
    }
)
NODE: Final = (
    "v22.19.0",
    "https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-x64.tar.xz",
    "c0649af18e6a24f6fe5535a3e86b341dd49a8e71117c8b68bde973ef834f16f2",
    "https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-arm64.tar.xz",
    "0b2d9f564b6594222a62c82e1df2efe119dd4a4aff29644f4dd325bf360b6bcc",
)
CPYTHON: Final = (
    "3.13.12+20260303",
    "https://github.com/astral-sh/python-build-standalone/releases/download/20260303/"
    "cpython-3.13.12%2B20260303-x86_64-unknown-linux-gnu-install_only.tar.gz",
    "ee1d08c3e6149343077d410d7de835a7a7dda8e2cec80df94f393e858a300055",
    "https://github.com/astral-sh/python-build-standalone/releases/download/20260303/"
    "cpython-3.13.12%2B20260303-aarch64-unknown-linux-gnu-install_only.tar.gz",
    "3b88fc6dd6f0290f075353ca860b9dc48faca0d849a0a03c088883507ed63881",
)
UV: Final = (
    "0.11.29",
    "https://github.com/astral-sh/uv/releases/download/0.11.29/uv-x86_64-unknown-linux-gnu.tar.gz",
    "04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829",
    "https://github.com/astral-sh/uv/releases/download/0.11.29/uv-aarch64-unknown-linux-gnu.tar.gz",
    "94500fb064ae3c971a873cba64d94694c50677e0a4dbf78735c80509e7429919",
)
COST_FIELDS: Final = (
    ("dokploy_scalar_input_per_million", "pricing.prompt"),
    ("dokploy_scalar_output_per_million", "pricing.completion"),
    ("dokploy_scalar_cache_read_per_million", "pricing.input_cache_read"),
    ("dokploy_scalar_cache_write_per_million", "pricing.input_cache_write"),
)
LIMITS: Final = (
    ("max_input_tokens", "context_length"),
    ("max_output_tokens", "max_completion_tokens"),
)
PROVENANCE: Final = (
    ("source_id", "provenance.source_id"),
    ("merged_decision_sha256", "provenance.merged_decision_sha256"),
    (
        "dokploy_pricing_selection_sha256",
        "provenance.dokploy_pricing_selection_sha256",
    ),
)
