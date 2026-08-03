from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.dokploy import coder_runtime_asset_archives
from dokploy_wizard.dokploy.coder_runtime_asset_archives import extract_runtime_archive
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_patches import (
    apply_kdense_patches,
    verify_kdense_patch_files,
)
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_source import (
    verify_canonical_kdense_manifest,
    verify_kdense_source_archive,
)
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import KdenseRuntimeContract
from dokploy_wizard.dokploy.coder_runtime_asset_manifest import (
    load_runtime_manifest,
    runtime_manifest_path,
)
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError
from tests.integration.kdense_pinned_source_remote import (
    _failure_stage,
    _script,
    run_remote_kdense_production_build,
)
from tests.integration.kdense_pinned_source_support import download_pinned_kdense_archive

UnsafeArchiveKind = Literal["traversal", "link", "special", "duplicate", "wrong_root"]


@pytest.fixture
def kdense_contract() -> KdenseRuntimeContract:
    return load_runtime_manifest(runtime_manifest_path()).kdense


@pytest.fixture
def kdense_archive(tmp_path: Path, kdense_contract: KdenseRuntimeContract) -> Path:
    return download_pinned_kdense_archive(tmp_path / "kdense.tar.gz", kdense_contract)


def test_kdense_source_build_contract_is_checked_in() -> None:
    # Given
    root = Path(__file__).resolve().parents[2]
    manifest_path = root / "src" / "dokploy_wizard" / "runtime-manifest.lock.json"

    # When
    document = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Then
    assert document["schema_version"] == 3
    runtime = document["workspace_runtime"]
    assert isinstance(runtime, dict)
    assert "kdense" in runtime


def test_kdense_fresh_build_uses_prebuilt_pinned_runtime() -> None:
    # Given
    root = Path(__file__).resolve().parents[2]
    template = (
        root / "templates/coder/default-ubuntu-code-server-kdense-byok/main.tf"
    ).read_text(encoding="utf-8")

    # When / Then
    assert 'KDENSE_WIZARD_CENTRAL_ONLY="1"' in template
    assert "workspace-catalog-sync.pyz --adapter kdense" in template
    assert "/opt/dokploy-wizard/runtime/install/kdense/source" in template
    assert "git clone" not in template
    assert "curl -fsSL" not in template
    assert "npm install" not in template
    assert "uv python install" not in template
    assert "prep_sandbox" not in template
    assert "gemini" not in template.lower()
    assert "ollama" not in template.lower()
    assert "fusion" not in template.lower()
    assert "pkill -f" not in template


def test_pinned_source_layout_and_canonical_manifest(
    kdense_archive: Path, kdense_contract: KdenseRuntimeContract
) -> None:
    # Given
    manifest_path = runtime_manifest_path()

    # When
    evidence = verify_kdense_source_archive(kdense_archive, kdense_contract)
    verify_canonical_kdense_manifest(manifest_path, kdense_archive, kdense_contract)

    # Then
    assert evidence.source_paths == kdense_contract.source_paths
    assert evidence.uv_lock_paths == ()
    assert tuple(path.path for path in kdense_contract.source_paths) == (
        "server/package-lock.json",
        "server/src/helpers/pyproject.toml",
        "server/src/prep.ts",
        "server/src/sandbox-seed.ts",
        "start.mjs",
        "web/package-lock.json",
    )


@pytest.mark.central_catalog_only
@pytest.mark.four_costs
@pytest.mark.unknown_or_unpriced
@pytest.mark.fusion_ollama_subagent_speech_blocked
@pytest.mark.direct_route_zero_calls
def test_pinned_production_build_remote_patch_behavior(
    kdense_contract: KdenseRuntimeContract,
) -> None:
    # Given
    if os.environ.get("KDENSE_PIN_NETWORK_TEST") != "1":
        script = _script(kdense_contract, "/tmp/dokploy-wizard-task7-local-contract")
        assert "TASK7_STATUS=0" in script
        return
    root = Path(__file__).resolve().parents[2]

    # When / Then
    run_remote_kdense_production_build(root, kdense_contract)


def test_patches_are_byte_identical_and_apply_in_required_order(
    tmp_path: Path, kdense_archive: Path, kdense_contract: KdenseRuntimeContract
) -> None:
    # Given
    source = tmp_path / "source"
    extract_runtime_archive(
        archive_path=kdense_archive,
        destination=source,
        expected_root=kdense_contract.archive_root,
    )
    repository_root = Path(__file__).resolve().parents[2]
    patches = verify_kdense_patch_files(repository_root, kdense_contract)

    # When
    apply_kdense_patches(source, patches)

    # Then
    models = (source / "server" / "src" / "agent" / "models.ts").read_text(encoding="utf-8")
    assert "costCacheRead" in models
    assert "wizardCentralOnly" in models


def test_patch_hash_or_order_rejects_central_only_before_pricing_cache(
    tmp_path: Path, kdense_archive: Path, kdense_contract: KdenseRuntimeContract
) -> None:
    # Given
    source = tmp_path / "source"
    extract_runtime_archive(
        archive_path=kdense_archive,
        destination=source,
        expected_root=kdense_contract.archive_root,
    )
    repository_root = Path(__file__).resolve().parents[2]
    patches = verify_kdense_patch_files(repository_root, kdense_contract)

    # When / Then
    with pytest.raises(RuntimeAssetError, match="patch order"):
        apply_kdense_patches(source, tuple(reversed(patches)))


def test_kdense_lock_drift_rejects_the_manifest(tmp_path: Path) -> None:
    # Given
    document = json.loads(runtime_manifest_path().read_text(encoding="utf-8"))
    document["workspace_runtime"]["kdense"]["tools"]["node"]["amd64"]["sha256"] = "0" * 64
    drifted = tmp_path / "runtime-manifest.lock.json"
    drifted.write_text(json.dumps(document), encoding="utf-8")

    # When / Then
    with pytest.raises(RuntimeAssetError, match="Node lock"):
        load_runtime_manifest(drifted)


def test_legacy_cost_input_and_output_names_reject_the_manifest(tmp_path: Path) -> None:
    # Given
    document = json.loads(runtime_manifest_path().read_text(encoding="utf-8"))
    costs = document["workspace_runtime"]["kdense"]["catalog"]["cost_fields"]
    assert isinstance(costs, list)
    first = costs[0]
    second = costs[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    first["source"] = "dokploy_scalar_input"
    second["source"] = "dokploy_scalar_output"
    drifted = tmp_path / "runtime-manifest.lock.json"
    drifted.write_text(json.dumps(document), encoding="utf-8")

    # When / Then
    with pytest.raises(RuntimeAssetError, match="cost mapping"):
        load_runtime_manifest(drifted)


def test_kdense_node_sha_runner_exports_pinned_node_on_path(
    kdense_contract: KdenseRuntimeContract,
) -> None:
    # Given
    script = _script(kdense_contract, "/tmp/task7")

    # When / Then
    assert 'PATH="$root/$node_root/bin:$PATH"' in script


def test_bounded_noisy_failure_never_returns_unknown() -> None:
    # Given
    stderr = b"x" * 1024

    # When
    stage = _failure_stage(stderr)

    # Then
    assert stage == "TASK7_FAILURE=unclassified"


def test_fabricated_upstream_uv_lock_rejects_source_layout(
    tmp_path: Path, kdense_contract: KdenseRuntimeContract
) -> None:
    # Given
    archive = tmp_path / "fabricated-uv-lock.tar.gz"
    _write_archive(
        archive,
        kdense_contract.archive_root,
        (("uv.lock", b"version = 1\n"),),
    )
    altered = replace(kdense_contract, archive_sha256=_sha256(archive))

    # When / Then
    with pytest.raises(RuntimeAssetError, match="uv lock inventory"):
        verify_kdense_source_archive(archive, altered)


@pytest.mark.parametrize("kind", ("traversal", "link", "special", "duplicate", "wrong_root"))
def test_source_layout_drift_rejects_unsafe_archive_members(
    tmp_path: Path, kind: UnsafeArchiveKind
) -> None:
    # Given
    archive = tmp_path / f"{kind}.tar.gz"
    root = "k-dense-byok-test"
    with tarfile.open(archive, "w:gz") as opened:
        _add_unsafe_member(opened, kind, root)

    # When / Then
    with pytest.raises(RuntimeAssetError, match="archive"):
        extract_runtime_archive(
            archive_path=archive,
            destination=tmp_path / "source",
            expected_root=root,
        )


def test_source_layout_drift_rejects_member_and_size_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    archive = tmp_path / "limits.tar.gz"
    root = "k-dense-byok-test"
    _write_archive(archive, root, (("one", b"a"), ("two", b"b")))
    monkeypatch.setattr(coder_runtime_asset_archives, "_MAX_ARCHIVE_MEMBERS", 2)

    # When / Then
    with pytest.raises(RuntimeAssetError, match="member count"):
        extract_runtime_archive(
            archive_path=archive,
            destination=tmp_path / "members",
            expected_root=root,
        )

    monkeypatch.setattr(coder_runtime_asset_archives, "_MAX_ARCHIVE_MEMBERS", 4)
    monkeypatch.setattr(coder_runtime_asset_archives, "_MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(RuntimeAssetError, match="too large"):
        extract_runtime_archive(
            archive_path=archive,
            destination=tmp_path / "size",
            expected_root=root,
        )


def _write_archive(archive: Path, root: str, files: tuple[tuple[str, bytes], ...]) -> None:
    with tarfile.open(archive, "w:gz") as opened:
        directory = tarfile.TarInfo(f"{root}/")
        directory.type = tarfile.DIRTYPE
        opened.addfile(directory)
        for name, content in files:
            member = tarfile.TarInfo(f"{root}/{name}")
            member.size = len(content)
            opened.addfile(member, io.BytesIO(content))


def _add_unsafe_member(opened: tarfile.TarFile, kind: UnsafeArchiveKind, root: str) -> None:
    match kind:
        case "traversal":
            member = tarfile.TarInfo(f"{root}/../escape")
            member.size = 1
            opened.addfile(member, io.BytesIO(b"x"))
            return
        case "link":
            member = tarfile.TarInfo(f"{root}/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "target"
            opened.addfile(member)
            return
        case "special":
            member = tarfile.TarInfo(f"{root}/pipe")
            member.type = tarfile.FIFOTYPE
            opened.addfile(member)
            return
        case "duplicate":
            _write_member(opened, f"{root}/same")
            _write_member(opened, f"{root}/same")
            return
        case "wrong_root":
            _write_member(opened, "other-root/file")
        case unreachable:
            assert_never(unreachable)


def _write_member(opened: tarfile.TarFile, name: str) -> None:
    member = tarfile.TarInfo(name)
    member.size = 1
    opened.addfile(member, io.BytesIO(b"x"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
