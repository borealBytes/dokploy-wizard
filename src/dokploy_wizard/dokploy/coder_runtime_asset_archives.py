"""Safe extraction for immutable Coder runtime archives."""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path, PurePosixPath
from typing import Final

from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError

_MAX_ARCHIVE_MEMBERS: Final = 4096
_MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024 * 1024


def extract_runtime_archive(
    *, archive_path: Path, destination: Path, expected_root: str
) -> None:
    """Extract a single-root regular-file archive without links or unsafe paths."""

    if destination.exists():
        raise RuntimeAssetError("runtime archive destination already exists")
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            _validate_members(members, expected_root)
            destination.mkdir(parents=True)
            for member in members:
                _write_member(archive, member, destination, expected_root)
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeAssetError("runtime archive is invalid") from error
    except RuntimeAssetError:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def extract_node_archive(*, archive_path: Path, destination: Path, expected_root: str) -> None:
    """Extract Node regular files and reconstruct only its validated npm entry links."""

    if destination.exists():
        raise RuntimeAssetError("runtime archive destination already exists")
    links = {
        "bin/npm": "../lib/node_modules/npm/bin/npm-cli.js",
        "bin/npx": "../lib/node_modules/npm/bin/npx-cli.js",
    }
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            _validate_node_members(members, expected_root, links)
            destination.mkdir(parents=True)
            for member in members:
                if not member.issym():
                    _write_member(archive, member, destination, expected_root)
            for relative, target in links.items():
                link_path = destination / relative
                source = link_path.parent / target
                if not source.is_file():
                    raise RuntimeAssetError("Node archive link target is missing")
                link_path.symlink_to(target)
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeAssetError("Node archive is invalid") from error
    except RuntimeAssetError:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def extract_zellij_binary_archive(*, archive_path: Path, destination: Path) -> None:
    """Extract the locked Zellij release's one root-level regular binary."""

    if destination.exists():
        raise RuntimeAssetError("runtime archive destination already exists")
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            if len(members) != 1 or members[0].name != "zellij" or not members[0].isfile():
                raise RuntimeAssetError("Zellij archive layout is invalid")
            destination.mkdir(parents=True)
            source = archive.extractfile(members[0])
            if source is None:
                raise RuntimeAssetError("Zellij archive binary is unreadable")
            target = destination / "bin" / "zellij"
            target.parent.mkdir()
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755)
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeAssetError("Zellij archive is invalid") from error
    except RuntimeAssetError:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _validate_members(members: list[tarfile.TarInfo], expected_root: str) -> None:
    if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
        raise RuntimeAssetError("runtime archive member count is invalid")
    destinations: set[str] = set()
    total_size = 0
    for member in members:
        destination = _member_destination(member, expected_root)
        destination_key = "." if destination is None else destination.as_posix()
        if destination_key in destinations:
            raise RuntimeAssetError("runtime archive has duplicate destinations")
        destinations.add(destination_key)
        total_size += member.size
        if total_size > _MAX_ARCHIVE_BYTES:
            raise RuntimeAssetError("runtime archive is too large")


def _validate_node_members(
    members: list[tarfile.TarInfo], expected_root: str, links: dict[str, str]
) -> None:
    if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
        raise RuntimeAssetError("Node archive member count is invalid")
    destinations: set[str] = set()
    total_size = 0
    for member in members:
        if member.issym():
            name = member.name.removeprefix(f"{expected_root}/")
            if name not in links or member.linkname != links[name]:
                raise RuntimeAssetError("Node archive link is invalid")
            destination = name
        else:
            target = _member_destination(member, expected_root)
            destination = "." if target is None else target.as_posix()
        if destination in destinations:
            raise RuntimeAssetError("Node archive has duplicate destinations")
        destinations.add(destination)
        total_size += member.size
        if total_size > _MAX_ARCHIVE_BYTES:
            raise RuntimeAssetError("Node archive is too large")
    if not set(links).issubset(destinations):
        raise RuntimeAssetError("Node archive links are missing")


def _member_destination(
    member: tarfile.TarInfo, expected_root: str
) -> PurePosixPath | None:
    name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
    path = PurePosixPath(name)
    if (
        not name
        or name != path.as_posix()
        or name.startswith("/")
        or "\\" in name
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != expected_root
    ):
        raise RuntimeAssetError("runtime archive path is invalid")
    if not member.isdir() and not member.isfile():
        raise RuntimeAssetError("runtime archive member type is invalid")
    relative = path.parts[1:]
    if not relative:
        if member.isfile():
            raise RuntimeAssetError("runtime archive root must be a directory")
        return None
    return PurePosixPath(*relative)


def _write_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, destination: Path, expected_root: str
) -> None:
    relative = _member_destination(member, expected_root)
    if relative is None:
        return
    target = destination / relative
    if member.isdir():
        target.mkdir(parents=True, exist_ok=False)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        raise RuntimeAssetError("runtime archive member is unreadable")
    with source, target.open("xb") as output:
        shutil.copyfileobj(source, output)
    target.chmod(0o755 if member.mode & 0o111 else 0o644)
