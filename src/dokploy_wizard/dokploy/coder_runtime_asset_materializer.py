"""Build deterministic offline Coder runtime assets from the immutable lock."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_manifest import load_runtime_manifest
from dokploy_wizard.dokploy.coder_runtime_asset_materialization import (
    MaterializationContext,
    materialize,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import (
    ARCHITECTURES,
    JsonValue,
    RuntimeAssetError,
    require_mapping,
)


def materialize_runtime_assets(
    *, manifest_path: Path, architecture: str, output_dir: Path
) -> None:
    """Materialize verified, install-ready assets for one supported architecture."""

    if architecture not in ARCHITECTURES:
        raise RuntimeAssetError("runtime architecture is invalid")
    load_runtime_manifest(manifest_path)
    runtime = _runtime_payload(manifest_path)
    with tempfile.TemporaryDirectory(
        prefix="dokploy-wizard-runtime-", dir=output_dir.parent
    ) as tmp:
        staged = Path(tmp) / "runtime"
        materialize(
            MaterializationContext(root=staged, runtime=runtime, architecture=architecture)
        )
        _publish(staged, output_dir)


def _runtime_payload(manifest_path: Path) -> Mapping[str, JsonValue]:
    try:
        payload: JsonValue = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeAssetError("runtime manifest is unreadable") from error
    document = require_mapping(payload, "runtime manifest")
    return require_mapping(document["workspace_runtime"], "workspace runtime")

def _publish(staged: Path, output_dir: Path) -> None:
    if output_dir.exists():
        raise RuntimeAssetError("runtime asset output already exists")
    os.replace(staged, output_dir)


def main(argv: list[str]) -> int:
    """Run the materializer command-line entrypoint without leaking tool output."""

    if len(argv) != 4:
        return 64
    try:
        materialize_runtime_assets(
            manifest_path=Path(argv[1]), architecture=argv[2], output_dir=Path(argv[3])
        )
    except RuntimeAssetError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
