from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path


def render_opencode_go_sync_package() -> bytes:
    package_root = Path(__file__).resolve().parents[1]
    litellm_root = package_root / "litellm"
    output = BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        _write_member(archive, "dokploy_wizard/__init__.py", b"")
        _write_member(archive, "dokploy_wizard/litellm/__init__.py", b"")
        for source in sorted(litellm_root.glob("*.py")):
            if source.name == "__init__.py":
                continue
            _write_member(
                archive,
                f"dokploy_wizard/litellm/{source.name}",
                source.read_bytes(),
            )
        _write_member(
            archive,
            "__main__.py",
            b"from dokploy_wizard.litellm.opencode_go_sync_runtime import main\n"
            b"raise SystemExit(main())\n",
        )
    return output.getvalue()


def _write_member(archive: zipfile.ZipFile, path: str, content: bytes) -> None:
    metadata = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    metadata.create_system = 3
    metadata.external_attr = 0o100644 << 16
    archive.writestr(
        metadata,
        content,
        compress_type=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    )
