from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from dokploy_wizard.dokploy import coder_runtime_asset_materialization as materialization
from dokploy_wizard.dokploy.coder_runtime_asset_materialization import MaterializationContext
from dokploy_wizard.dokploy.coder_runtime_asset_types import JsonValue


def test_pi_install_replaces_packaged_shrinkwrap_with_verified_release_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    packaged_shrinkwrap = b'{"name":"packaged"}\n'
    verified_release_lock = b'{"name":"verified-release"}\n'
    runtime: dict[str, JsonValue] = {
        "pi": {
            "tarball_url": "https://example.invalid/pi.tgz",
            "tarball_sha512": hashlib.sha512(b"tarball").digest().hex(),
            "shrinkwrap_sha256": hashlib.sha256(packaged_shrinkwrap).hexdigest(),
            "package_lock_url": "https://example.invalid/package-lock.json",
            "package_lock_sha256": hashlib.sha256(verified_release_lock).hexdigest(),
        }
    }
    context = MaterializationContext(
        root=tmp_path / "runtime", runtime=runtime, architecture="amd64"
    )

    def download(
        _: MaterializationContext, spec: materialization._DownloadSpec
    ) -> None:
        spec.destination.parent.mkdir(parents=True, exist_ok=True)
        if spec.destination.name == "pi.tgz":
            with tarfile.open(spec.destination, "w:gz") as archive:
                root = tarfile.TarInfo("package/")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                shrinkwrap = tarfile.TarInfo("package/npm-shrinkwrap.json")
                shrinkwrap.size = len(packaged_shrinkwrap)
                archive.addfile(shrinkwrap, io.BytesIO(packaged_shrinkwrap))
        else:
            spec.destination.write_bytes(verified_release_lock)

    monkeypatch.setattr(materialization, "_download", download)
    monkeypatch.setattr(materialization, "_install_pi_dependencies", lambda *_: None)

    # When
    materialization._materialize_pi(context, tmp_path / "node" / "bin" / "node")

    # Then
    destination = context.install / "pi"
    assert (destination / "npm-shrinkwrap.json").read_bytes() == verified_release_lock
    assert not (destination / "package-lock.json").exists()
