"""Verify the pinned scientific-skills source without mutable Git resolution."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from dokploy_wizard.dokploy.coder_runtime_asset_archives import extract_runtime_archive
from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import KdenseSkillSource
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError

_MAX_BYTES = 256 * 1024 * 1024
_API = "https://api.github.com/repos/K-Dense-AI/scientific-agent-skills"


def verify_kdense_skills(source: KdenseSkillSource) -> None:
    """Verify immutable Git objects, codeload bytes, and GNU-framed skills digest."""

    if source.repository != "K-Dense-AI/scientific-agent-skills":
        raise RuntimeAssetError("K-Dense skills repository is invalid")
    commit = _json(f"{_API}/git/commits/{source.commit}")
    commit_tree = _mapping(_mapping(commit, "skills commit").get("tree"), "skills commit tree")
    if _text(commit_tree.get("sha"), "skills commit tree") != source.tree:
        raise RuntimeAssetError("K-Dense skills tree is invalid")
    tree = _json(f"{_API}/git/trees/{source.tree}")
    entries = _mapping(tree, "skills tree").get("tree")
    if not isinstance(entries, list):
        raise RuntimeAssetError("K-Dense skills subtree is invalid")
    skill = next(
        (
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("path") == "skills"
        ),
        None,
    )
    if (
        not isinstance(skill, dict)
        or skill.get("type") != "tree"
        or skill.get("sha") != source.subtree
    ):
        raise RuntimeAssetError("K-Dense skills subtree is invalid")
    with tempfile.TemporaryDirectory(prefix="dokploy-wizard-kdense-skills-") as temporary:
        root = Path(temporary)
        archive = _download(source.archive_url, root / "skills.tar.gz", source.archive_sha256)
        extracted = root / "source"
        extract_runtime_archive(
            archive_path=archive,
            destination=extracted,
            expected_root=f"scientific-agent-skills-{source.commit}",
        )
        if _canonical(extracted / "skills") != source.canonical_sha256:
            raise RuntimeAssetError("K-Dense skills canonical digest is invalid")


def _json(url: str) -> dict[str, object]:
    raw = _read(url)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeAssetError("K-Dense skills API response is invalid") from error
    return _mapping(value, "skills API response")


def _download(url: str, destination: Path, expected: str) -> Path:
    data = _read(url)
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeAssetError("K-Dense skills archive checksum is invalid")
    destination.write_bytes(data)
    return destination


def _read(url: str) -> bytes:
    request = Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            if response.url != url:
                raise RuntimeAssetError("K-Dense skills redirect is invalid")
            data = response.read(_MAX_BYTES + 1)
    except OSError as error:
        raise RuntimeAssetError("K-Dense skills download is unavailable") from error
    if len(data) > _MAX_BYTES:
        raise RuntimeAssetError("K-Dense skills download exceeds bound")
    if not isinstance(data, bytes):
        raise RuntimeAssetError("K-Dense skills download is invalid")
    return data


def _canonical(skills: Path) -> str:
    if not skills.is_dir() or skills.is_symlink():
        raise RuntimeAssetError("K-Dense skills layout is invalid")
    records = bytearray()
    ordered = sorted(
        skills.rglob("*"), key=lambda item: item.relative_to(skills.parent).as_posix().encode()
    )
    for path in ordered:
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(skills.parent).as_posix().encode("ascii")
        records.extend(hashlib.sha256(path.read_bytes()).hexdigest().encode())
        records.extend(b"  " + relative + b"\n")
    if not records:
        raise RuntimeAssetError("K-Dense skills layout is invalid")
    return hashlib.sha256(records).hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeAssetError(f"K-Dense {label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeAssetError(f"K-Dense {label} is invalid")
    return value
