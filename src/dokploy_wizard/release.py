"""Commit-addressed deployment archive and activation entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dokploy_wizard.release_activation import ReleaseActivation, activate_release
from dokploy_wizard.release_manifest import (
    ReleaseError,
    ReleaseManifest,
    manifest_from_archive,
    read_release_manifest,
    write_release_manifest,
)

PROTECTED_DEPLOYABLE_PATHS = (
    "src",
    "templates",
    "docker",
    "bin",
    "scripts",
    "pyproject.toml",
    "pytest.ini",
    "README.md",
    "AGENTS.md",
)
ACTIVATION_BOOTSTRAP_MEMBER = "scripts/release_activation_bootstrap.py"


class GitRunner(Protocol):
    """Boundary for the three value-free Git archive commands."""

    def __call__(self, command: tuple[str, ...]) -> str: ...


@dataclass(frozen=True, slots=True)
class CommitArchiveEvidence:
    """Archive identity and its exact, secret-free file manifest."""

    commit_sha: str
    archive_sha256: str
    manifest: ReleaseManifest
    bootstrap_bytes: bytes
    bootstrap_sha256: str

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256


def create_commit_archive(
    *, repo_root: Path, deploy_commit: str, destination: Path, run_git: GitRunner | None = None
) -> CommitArchiveEvidence:
    """Reject deployable worktree drift, then archive only one resolved Git commit."""

    runner = _SubprocessGitRunner(repo_root) if run_git is None else run_git
    status = runner(
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *PROTECTED_DEPLOYABLE_PATHS,
        )
    )
    if status:
        raise ReleaseError("deployable drift prevents commit-addressed packaging")
    resolved_commit = runner(("rev-parse", "--verify", f"{deploy_commit}^{{commit}}")).strip()
    if not resolved_commit:
        raise ReleaseError("deploy commit cannot be resolved")
    runner(("archive", "--format=tar.gz", f"--output={destination.resolve()}", resolved_commit))
    manifest = manifest_from_archive(resolved_commit, destination)
    bootstrap_bytes = _archive_member_bytes(destination, ACTIVATION_BOOTSTRAP_MEMBER)
    bootstrap_sha256 = hashlib.sha256(bootstrap_bytes).hexdigest()
    entry = next(
        (item for item in manifest.files if item.path == ACTIVATION_BOOTSTRAP_MEMBER), None
    )
    if entry is None or entry.sha256 != bootstrap_sha256:
        raise ReleaseError("release activation bootstrap is not archive-bound")
    return CommitArchiveEvidence(
        resolved_commit, manifest.archive_sha256, manifest, bootstrap_bytes, bootstrap_sha256
    )


def _archive_member_bytes(archive_path: Path, member_name: str) -> bytes:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            member = archive.getmember(member_name)
            if not member.isreg():
                raise ReleaseError("release activation bootstrap is unsafe")
            stream = archive.extractfile(member)
            if stream is None:
                raise ReleaseError("release activation bootstrap is unreadable")
            with stream:
                content = stream.read(member.size + 1)
    except (KeyError, OSError, tarfile.TarError) as error:
        raise ReleaseError("release activation bootstrap is unavailable") from error
    if len(content) != member.size:
        raise ReleaseError("release activation bootstrap is truncated")
    return content


def build_parser() -> argparse.ArgumentParser:
    """Build the remote activation-only release command parser."""

    parser = argparse.ArgumentParser(prog="python -m dokploy_wizard.release")
    subparsers = parser.add_subparsers(dest="command", required=True)
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--archive", type=Path, required=True)
    activate_parser.add_argument("--manifest", type=Path, required=True)
    activate_parser.add_argument("--releases-root", type=Path, required=True)
    activate_parser.add_argument("--active-link", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Activate a pre-extracted transfer archive without consulting a worktree."""

    args = build_parser().parse_args(argv)
    manifest = read_release_manifest(args.manifest)
    activation = activate_release(
        archive_path=args.archive,
        manifest=manifest,
        releases_root=args.releases_root,
        active_link=args.active_link,
    )
    print(activation.manifest_sha256)
    return 0


@dataclass(frozen=True, slots=True)
class _SubprocessGitRunner:
    repo_root: Path

    def __call__(self, command: tuple[str, ...]) -> str:
        try:
            result = subprocess.run(
                ("git", "-C", str(self.repo_root), *command),
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise ReleaseError("git archive command is unavailable") from error
        if result.returncode != 0:
            raise ReleaseError("git archive command failed")
        return result.stdout


__all__ = [
    "CommitArchiveEvidence",
    "ACTIVATION_BOOTSTRAP_MEMBER",
    "PROTECTED_DEPLOYABLE_PATHS",
    "ReleaseActivation",
    "ReleaseError",
    "ReleaseManifest",
    "activate_release",
    "create_commit_archive",
    "manifest_from_archive",
    "write_release_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
