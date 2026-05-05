# pyright: reportIncompatibleMethodOverride=false

"""Remote lifecycle CLI contract for dokploy-wizard."""

from __future__ import annotations

import argparse
import posixpath
import shlex
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

from dokploy_wizard.remote_transport import (
    ParamikoRemoteTransport,
    RemoteCommandFailure,
    RemoteTransportSession,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dokploy-wizard-remote",
        description=(
            "Run remote lifecycle commands against a target host. Defaults assume deployment "
            "under /root/dokploy-wizard using .install.env. Fresh mode is destructive and "
            "confirm-file-gated."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install",
        help="install the wizard-managed stack on a remote host",
        description=(
            "Upload the repo and run install on the remote host. Defaults: --user root, "
            "--remote-path /root/dokploy-wizard, --env-file .install.env. --fresh is destructive "
            "and requires --confirm-file before any remote action."
        ),
    )
    _add_remote_common_arguments(install_parser)
    _add_fresh_arguments(install_parser)

    modify_parser = subparsers.add_parser(
        "modify",
        help="run modify against a remote host",
        description=(
            "Upload the repo and run modify remotely. Defaults: --user root, --remote-path "
            "/root/dokploy-wizard, --env-file .install.env. --fresh is destructive and requires "
            "--confirm-file before any remote action."
        ),
    )
    _add_remote_common_arguments(modify_parser)
    _add_fresh_arguments(modify_parser)

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="run uninstall against a remote host",
        description=(
            "Run uninstall remotely. Defaults: --user root, --remote-path /root/dokploy-wizard, "
            "--env-file .install.env. Use --confirm-file for destructive confirmation flows."
        ),
    )
    _add_remote_common_arguments(uninstall_parser)
    uninstall_parser.add_argument(
        "--fresh",
        action="store_true",
        help="invalid for uninstall; provided only so the CLI can reject it clearly",
    )
    uninstall_mode = uninstall_parser.add_mutually_exclusive_group()
    uninstall_mode.add_argument(
        "--retain-data",
        action="store_true",
        help="remove wizard-managed resources while retaining data-bearing resources",
    )
    uninstall_mode.add_argument(
        "--destroy-data",
        action="store_true",
        help="remove all wizard-managed resources, including data-bearing resources",
    )
    uninstall_parser.add_argument(
        "--confirm-file",
        type=Path,
        help="path to a typed confirmation file used for destructive remote actions",
    )

    inspect_parser = subparsers.add_parser(
        "inspect-state",
        help="inspect remote wizard state",
        description=(
            "Inspect remote state without lifecycle mutation. Defaults: --user root, "
            "--remote-path /root/dokploy-wizard, --env-file .install.env."
        ),
    )
    _add_remote_common_arguments(inspect_parser)

    proof_parser = subparsers.add_parser(
        "proof",
        help="run remote proof flow",
        description=(
            "Run the remote proof flow. Defaults: --user root, --remote-path /root/dokploy-wizard, "
            "--env-file .install.env. --fresh is destructive and requires --confirm-file before "
            "any remote action."
        ),
    )
    _add_remote_common_arguments(proof_parser)
    _add_fresh_arguments(proof_parser)

    parser.epilog = (
        "Lifecycle commands: install, modify, uninstall, inspect-state, proof. "
        "Remote defaults: /root/dokploy-wizard and .install.env."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    _validate_runtime_args(parser, args)

    if args.command in {"install", "modify", "uninstall", "proof"}:
        _require_local_env_file(args.env_file)

    try:
        transport = ParamikoRemoteTransport.connect(
            hostname=args.host,
            username=args.user,
            password=args.password,
            remote_root=str(args.remote_path),
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(_redact_runtime_message(str(error), password=args.password), file=sys.stderr)
        return 1

    session = RemoteTransportSession(transport=transport, remote_root=str(args.remote_path))

    try:
        if args.command in {"install", "modify", "uninstall", "proof"}:
            _upload_remote_bundle(args=args, session=session)
            _extract_remote_bundle(session=session, password=args.password)
        if args.command == "install":
            if args.fresh:
                remote_confirm_path = _upload_confirm_file(
                    session=session,
                    confirm_file=args.confirm_file,
                )
                _run_remote_command(
                    session=session,
                    subcommand="uninstall",
                    command=_build_uninstall_command(
                        session,
                        destroy_data=True,
                        remote_confirm_path=remote_confirm_path,
                    ),
                    password=args.password,
                )
            _run_remote_command(
                session=session,
                subcommand="install",
                command=_build_install_command(session),
                password=args.password,
            )
            return 0
        if args.command == "modify":
            if args.fresh:
                remote_confirm_path = _upload_confirm_file(
                    session=session,
                    confirm_file=args.confirm_file,
                )
                _run_remote_command(
                    session=session,
                    subcommand="uninstall",
                    command=_build_uninstall_command(
                        session,
                        destroy_data=True,
                        remote_confirm_path=remote_confirm_path,
                    ),
                    password=args.password,
                )
            _run_remote_command(
                session=session,
                subcommand="modify",
                command=_build_modify_command(session),
                password=args.password,
            )
            return 0
        if args.command == "uninstall":
            remote_confirm_path = _upload_confirm_file(
                session=session,
                confirm_file=args.confirm_file,
            )
            _run_remote_command(
                session=session,
                subcommand="uninstall",
                command=_build_uninstall_command(
                    session,
                    destroy_data=args.destroy_data,
                    remote_confirm_path=remote_confirm_path,
                ),
                password=args.password,
            )
            return 0
        if args.command == "inspect-state":
            _run_remote_command(
                session=session,
                subcommand="inspect-state",
                command=_build_inspect_state_command(session),
                password=args.password,
            )
            return 0

        assert args.command == "proof"
        if args.fresh:
            _upload_confirm_file(session=session, confirm_file=args.confirm_file)
        session.run_proof(
            password=args.password,
            fresh=args.fresh,
            confirm_file=args.confirm_file,
        )
        return 0
    except (OSError, RemoteCommandFailure, RuntimeError, ValueError) as error:
        print(_redact_runtime_message(str(error), password=args.password), file=sys.stderr)
        return 1
    finally:
        transport.close()


def _add_remote_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, help="target host or IP address")
    parser.add_argument("--password", help="target user password")
    parser.add_argument(
        "--user",
        default="root",
        help="remote SSH username (default: root)",
    )
    parser.add_argument(
        "--remote-path",
        type=Path,
        default=Path("/root/dokploy-wizard"),
        help="remote repo path (default: /root/dokploy-wizard)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".install.env"),
        help="install env file relative to the repo root (default: .install.env)",
    )


def _add_fresh_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="DESTRUCTIVE: rebuild remote path from scratch; requires --confirm-file",
    )
    parser.add_argument(
        "--confirm-file",
        type=Path,
        help="typed confirmation file required for destructive fresh mode",
    )


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    command = getattr(args, "command", None)
    if command == "uninstall" and getattr(args, "fresh", False):
        parser.error("--fresh is not supported for uninstall.")
    if command in {"install", "modify", "proof"} and getattr(args, "fresh", False):
        if getattr(args, "confirm_file", None) is None:
            parser.error(
                "--fresh is destructive and requires --confirm-file before any remote action."
            )
    if command == "uninstall":
        if not (getattr(args, "retain_data", False) or getattr(args, "destroy_data", False)):
            parser.error("uninstall requires either --retain-data or --destroy-data.")
        if getattr(args, "confirm_file", None) is None:
            parser.error(
                "uninstall requires --confirm-file for non-interactive remote confirmation."
            )


def _validate_runtime_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not getattr(args, "password", None):
        parser.error("--password is required for remote SSH authentication.")


def _require_local_env_file(env_file: Path) -> None:
    if not env_file.exists():
        raise FileNotFoundError(f"install env file does not exist: {env_file}")
    if not env_file.is_file():
        raise FileNotFoundError(f"install env file is not a regular file: {env_file}")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _upload_remote_bundle(*, args: argparse.Namespace, session: RemoteTransportSession) -> None:
    with tempfile.TemporaryDirectory(prefix="dokploy-wizard-remote-") as temp_dir:
        archive_path = Path(temp_dir) / "repo.tar.gz"
        _create_repo_archive(repo_root=_repo_root(), destination=archive_path)
        session.upload_bundle(repo_archive=archive_path, install_env_file=args.env_file)


def _extract_remote_bundle(*, session: RemoteTransportSession, password: str | None) -> None:
    _run_remote_command(
        session=session,
        subcommand="extract-repo",
        command=_build_extract_command(session),
        password=password,
    )


def _upload_confirm_file(*, session: RemoteTransportSession, confirm_file: Path) -> str:
    remote_confirm_path = _remote_path(session.remote_root, confirm_file)
    remote_confirm_dir = posixpath.dirname(remote_confirm_path)
    if remote_confirm_dir:
        session.transport.ensure_dir(remote_confirm_dir)
    session.transport.upload(confirm_file, remote_confirm_path)
    session.transport.chmod(remote_confirm_path, 0o600)
    return remote_confirm_path


def _build_extract_command(session: RemoteTransportSession) -> str:
    return _shell_join(
        [
            "tar",
            "-xzf",
            session.remote_archive_path,
            "-C",
            session.remote_root,
        ]
    )


def _build_install_command(session: RemoteTransportSession) -> str:
    return _shell_join(
        [
            "./bin/dokploy-wizard",
            "install",
            "--env-file",
            session.remote_install_env_path,
            "--state-dir",
            session.remote_state_dir,
            "--non-interactive",
        ]
    )


def _build_modify_command(session: RemoteTransportSession) -> str:
    return _shell_join(
        [
            "./bin/dokploy-wizard",
            "modify",
            "--env-file",
            session.remote_install_env_path,
            "--state-dir",
            session.remote_state_dir,
            "--non-interactive",
        ]
    )


def _build_uninstall_command(
    session: RemoteTransportSession,
    *,
    destroy_data: bool,
    remote_confirm_path: str,
) -> str:
    command = [
        "./bin/dokploy-wizard",
        "uninstall",
        "--state-dir",
        session.remote_state_dir,
        "--destroy-data" if destroy_data else "--retain-data",
        "--non-interactive",
        "--confirm-file",
        remote_confirm_path,
    ]
    return _shell_join(command)


def _build_inspect_state_command(session: RemoteTransportSession) -> str:
    return _shell_join(
        [
            "./bin/dokploy-wizard",
            "inspect-state",
            "--env-file",
            session.remote_install_env_path,
            "--state-dir",
            session.remote_state_dir,
        ]
    )


def _run_remote_command(
    *,
    session: RemoteTransportSession,
    subcommand: str,
    command: str,
    password: str | None,
) -> None:
    try:
        session.transport.run(subcommand, command)
    except RemoteCommandFailure:
        raise
    except Exception as error:
        raise RemoteCommandFailure(
            subcommand=subcommand,
            error=error,
            password=password,
        ) from error


def _remote_path(remote_root: str, path: Path) -> str:
    if path.is_absolute():
        return path.as_posix()
    return posixpath.join(remote_root.rstrip("/") or "/", path.as_posix())


def _shell_join(arguments: Sequence[str]) -> str:
    return " ".join(shlex.quote(argument) for argument in arguments)


def _redact_runtime_message(message: str, *, password: str | None) -> str:
    if not password:
        return message
    return message.replace(password, "<redacted>")


def _create_repo_archive(*, repo_root: Path, destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(repo_root.rglob("*")):
            relative = path.relative_to(repo_root)
            if _should_skip(relative):
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)


def _should_skip(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in {
        ".git",
        ".venv",
        "venv",
        "env",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".dokploy-wizard-state",
    }:
        return True
    if parts[0] == ".sisyphus" and len(parts) > 1 and parts[1] == "evidence":
        return True
    if relative.name in {".install.env", ".fresh-vps-validation.env", "openclaw-compose-fixed.yml"}:
        return True
    if relative.suffix == ".swp":
        return True
    return any(part == "__pycache__" for part in parts)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
