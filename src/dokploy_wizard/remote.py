# pyright: reportIncompatibleMethodOverride=false

"""Remote lifecycle CLI contract for dokploy-wizard."""

from __future__ import annotations

import argparse
import posixpath
import shlex
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from dokploy_wizard.remote_transport import (
    ParamikoRemoteTransport,
    RemoteCommandFailure,
    RemoteTransportSession,
)
from dokploy_wizard.verification import redact_text


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
            "Run the remote proof flow. By default this installs once, runs service verification, "
            "and then captures inspect-state. Use --strict-idempotency to rerun install as an "
            "explicit unchanged-healthy idempotency check. Defaults: --user root, --remote-path "
            "/root/dokploy-wizard, --env-file .install.env. --fresh is destructive and requires "
            "--confirm-file before any remote action."
        ),
    )
    _add_remote_common_arguments(proof_parser)
    _add_fresh_arguments(proof_parser)
    proof_parser.add_argument(
        "--strict-idempotency",
        action="store_true",
        help=(
            "rerun install after the verification phase to assert the unchanged healthy stack "
            "stays no-op"
        ),
    )

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

    reporter = _RemoteProgressReporter(
        verbose=args.verbose,
        password=args.password,
        stream=sys.stderr,
    )
    started = time.monotonic()
    reporter.progress(f"starting remote {args.command}")

    try:
        if args.command in {"install", "modify", "uninstall", "proof"}:
            _require_local_env_file(args.env_file)
    except (OSError, ValueError) as error:
        print(_redact_runtime_message(str(error), password=args.password), file=sys.stderr)
        reporter.finish(args.command, exit_code=1, started=started)
        return 1

    try:
        reporter.progress(
            f"connecting to {args.user}@{args.host}:22 path={args.remote_path}"
        )
        connect_started = time.monotonic()
        transport = ParamikoRemoteTransport.connect(
            hostname=args.host,
            username=args.user,
            password=args.password,
            remote_root=str(args.remote_path),
            verbose=args.verbose,
            output_callback=reporter.remote_output,
        )
        connect_elapsed = time.monotonic() - connect_started
        reporter.progress(f"connected over SSH ({connect_elapsed:.1f}s)")
    except (OSError, RuntimeError, ValueError) as error:
        print(_redact_runtime_message(str(error), password=args.password), file=sys.stderr)
        reporter.finish(args.command, exit_code=1, started=started)
        return 1

    session = RemoteTransportSession(
        transport=transport,
        remote_root=str(args.remote_path),
        progress_callback=reporter.progress,
    )
    exit_code = 1

    try:
        if args.command in {"install", "modify", "uninstall", "proof"}:
            _upload_remote_bundle(args=args, session=session, reporter=reporter)
            _extract_remote_bundle(session=session, password=args.password)
        if args.command == "install":
            if args.fresh:
                remote_confirm_path = _upload_confirm_file(
                    session=session,
                    confirm_file=args.confirm_file,
                    reporter=reporter,
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
            exit_code = 0
            return exit_code
        if args.command == "modify":
            if args.fresh:
                remote_confirm_path = _upload_confirm_file(
                    session=session,
                    confirm_file=args.confirm_file,
                    reporter=reporter,
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
            exit_code = 0
            return exit_code
        if args.command == "uninstall":
            remote_confirm_path = _upload_confirm_file(
                session=session,
                confirm_file=args.confirm_file,
                reporter=reporter,
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
            exit_code = 0
            return exit_code
        if args.command == "inspect-state":
            _run_remote_command(
                session=session,
                subcommand="inspect-state",
                command=_build_inspect_state_command(session),
                password=args.password,
            )
            exit_code = 0
            return exit_code

        assert args.command == "proof"
        if args.fresh:
            _upload_confirm_file(
                session=session,
                confirm_file=args.confirm_file,
                reporter=reporter,
            )
        session.run_proof(
            password=args.password,
            fresh=args.fresh,
            confirm_file=args.confirm_file,
            strict_idempotency=args.strict_idempotency,
        )
        exit_code = 0
        return exit_code
    except (OSError, RemoteCommandFailure, RuntimeError, ValueError) as error:
        print(_redact_runtime_message(str(error), password=args.password), file=sys.stderr)
        return 1
    finally:
        transport.close()
        reporter.progress("closed SSH connection")
        reporter.finish(args.command, exit_code=exit_code, started=started)


def _add_remote_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, help="target host or IP address")
    parser.add_argument("--password", help="target user password")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="stream remote stdout/stderr live with command labels",
    )
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


def _upload_remote_bundle(
    *,
    args: argparse.Namespace,
    session: RemoteTransportSession,
    reporter: "_RemoteProgressReporter",
) -> None:
    started = time.monotonic()
    reporter.progress("creating repo archive for upload")
    with tempfile.TemporaryDirectory(prefix="dokploy-wizard-remote-") as temp_dir:
        archive_path = Path(temp_dir) / "repo.tar.gz"
        _create_repo_archive(repo_root=_repo_root(), destination=archive_path)
        reporter.progress(
            "uploading repo archive and install env file "
            f"to {session.remote_root} (env contents redacted)"
        )
        session.upload_bundle(repo_archive=archive_path, install_env_file=args.env_file)
    elapsed = time.monotonic() - started
    reporter.progress(f"uploaded remote bundle ({elapsed:.1f}s)")


def _extract_remote_bundle(*, session: RemoteTransportSession, password: str | None) -> None:
    _run_remote_command(
        session=session,
        subcommand="extract-repo",
        command=_build_extract_command(session),
        password=password,
    )


def _upload_confirm_file(
    *,
    session: RemoteTransportSession,
    confirm_file: Path,
    reporter: "_RemoteProgressReporter | None" = None,
) -> str:
    remote_confirm_path = _remote_path(session.remote_root, confirm_file)
    remote_confirm_dir = posixpath.dirname(remote_confirm_path)
    started = time.monotonic()
    if reporter is not None:
        reporter.progress(f"uploading confirmation file to {remote_confirm_path}")
    if remote_confirm_dir:
        session.transport.ensure_dir(remote_confirm_dir)
    session.transport.upload(confirm_file, remote_confirm_path)
    session.transport.chmod(remote_confirm_path, 0o600)
    if reporter is not None:
        elapsed = time.monotonic() - started
        reporter.progress(f"uploaded confirmation file ({elapsed:.1f}s)")
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
    session.run_command(subcommand=subcommand, command=command, password=password)


def _remote_path(remote_root: str, path: Path) -> str:
    if path.is_absolute():
        return path.as_posix()
    return posixpath.join(remote_root.rstrip("/") or "/", path.as_posix())


def _shell_join(arguments: Sequence[str]) -> str:
    return " ".join(shlex.quote(argument) for argument in arguments)


def _redact_runtime_message(message: str, *, password: str | None) -> str:
    if password:
        message = message.replace(password, "<redacted>")
    return redact_text(message)


class _RemoteProgressReporter:
    def __init__(self, *, verbose: bool, password: str | None, stream: TextIO) -> None:
        self.verbose = verbose
        self.password = password
        self.stream = stream
        self._redact_next_remote_assignment: dict[tuple[str, str], bool] = {}

    def progress(self, message: str) -> None:
        print(f"[remote] {self._redact(message)}", file=self.stream)

    def remote_output(self, subcommand: str, stream_name: str, line: str) -> None:
        if not self.verbose:
            return
        line = self._redact_remote_output_line(subcommand, stream_name, line)
        print(
            f"[remote:{subcommand}:{stream_name}] {self._redact(line)}",
            file=self.stream,
        )

    def finish(self, command: str, *, exit_code: int, started: float) -> None:
        elapsed = time.monotonic() - started
        status = "completed" if exit_code == 0 else "failed"
        self.progress(f"remote {command} {status} ({elapsed:.1f}s)")

    def _redact(self, message: str) -> str:
        return _redact_runtime_message(message, password=self.password)

    def _redact_remote_output_line(
        self,
        subcommand: str,
        stream_name: str,
        line: str,
    ) -> str:
        key = (subcommand, stream_name)
        if self._redact_next_remote_assignment.get(key, False):
            self._redact_next_remote_assignment[key] = False
            if _looks_like_env_assignment(line):
                env_key = line.split("=", 1)[0]
                return f"{env_key}=<REDACTED>"
        if line.startswith("# dokploy-wizard-env"):
            self._redact_next_remote_assignment[key] = True
        return line


def _looks_like_env_assignment(line: str) -> bool:
    key, separator, _value = line.partition("=")
    return bool(separator and key and key.replace("_", "").isalnum())


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
    if _is_secret_env_artifact(relative.name):
        return True
    if relative.suffix == ".swp":
        return True
    return any(part == "__pycache__" for part in parts)


def _is_secret_env_artifact(name: str) -> bool:
    sensitive_env_files = {".install.env", ".fresh-vps-validation.env"}
    if name in sensitive_env_files:
        return True
    backup_suffixes = {"bak", "backup", "old", "orig", "save", "tmp"}
    return any(
        name == f"{env_name}.{suffix}"
        for env_name in sensitive_env_files
        for suffix in backup_suffixes
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
