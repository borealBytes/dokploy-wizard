# pyright: reportIncompatibleMethodOverride=false

"""Remote lifecycle CLI contract for dokploy-wizard."""

from __future__ import annotations

import argparse
import hashlib
import os
import posixpath
import shlex
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from dokploy_wizard.proof.model_sync_task1_context import (
    Task1ProofContextError,
    Task1ProofContextV1,
    activate_task1_proof_context,
    validate_task1_proof_context_argument,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    Task1RemoteProofBinding,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_remote_observation import (
    RemoteModifyObservationRunner,
)
from dokploy_wizard.release import (
    CommitArchiveEvidence,
    ReleaseError,
    create_commit_archive,
    write_release_manifest,
)
from dokploy_wizard.remote_transport import (
    ParamikoRemoteTransport,
    RemoteCapturedCommandFailure,
    RemoteCommandCaptureLimits,
    RemoteCommandFailure,
    RemoteTransportSession,
)
from dokploy_wizard.state import StateValidationError, parse_env_file, resolve_desired_state
from dokploy_wizard.verification import redact_text

TASK1_CLEANUP_CAPTURE_LIMITS: Final[RemoteCommandCaptureLimits] = RemoteCommandCaptureLimits(
    timeout_seconds=120.0,
    max_stdout_bytes=2 * 1024 * 1024,
    max_stderr_bytes=2 * 1024 * 1024,
)
_TASK1_CLEANUP_JOURNAL_ABSENT: Final = b"Task 1 Cloudflare cleanup journal is absent"


class RepositoryArchiveError(ReleaseError):
    """Raised when the committed repository tree cannot be archived safely."""


RepositoryArchiveEvidence = CommitArchiveEvidence


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
    modify_parser.add_argument(
        "--capture-upgrade-observations",
        action="store_true",
        help=argparse.SUPPRESS,
    )

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

    cleanup_parser = subparsers.add_parser(
        "task1-cleanup", help=argparse.SUPPRESS, description=argparse.SUPPRESS
    )
    _add_remote_common_arguments(cleanup_parser)

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

    task1_context: Task1ProofContextV1 | None = None
    try:
        if args.command in {"install", "modify", "uninstall", "proof", "task1-cleanup"}:
            _require_local_env_file(args.env_file)
        task1_context = _validate_task1_proof_context(args)
    except (OSError, Task1ProofContextError, ValueError) as error:
        print(_redact_runtime_message(str(error), password=args.password), file=sys.stderr)
        reporter.finish(args.command, exit_code=1, started=started)
        return 1

    try:
        reporter.progress(f"connecting to {args.user}@{args.host}:22 path={args.remote_path}")
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
        task1_proof_context=args.task1_proof_context,
        progress_callback=reporter.progress,
    )
    exit_code = 1

    archive_evidence: RepositoryArchiveEvidence | None = None
    try:
        if args.command in {"install", "modify", "uninstall", "proof"}:
            archive_evidence = _upload_remote_bundle(args=args, session=session, reporter=reporter)
            _extract_remote_bundle(
                session=session,
                archive_evidence=archive_evidence,
                password=args.password,
            )
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
            with activate_task1_proof_context(task1_context):
                _print_expected_service_urls(
                    reporter,
                    _resolve_expected_service_url_links(args.env_file),
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
            with activate_task1_proof_context(task1_context):
                _print_expected_service_urls(
                    reporter,
                    _resolve_expected_service_url_links(args.env_file),
                )
            command = _build_modify_command(
                session,
                capture_upgrade_observations=args.capture_upgrade_observations,
            )
            if args.capture_upgrade_observations:
                RemoteModifyObservationRunner(session, reporter.remote_output).run(
                    command,
                    args.password,
                )
            else:
                _run_remote_command(
                    session=session,
                    subcommand="modify",
                    command=command,
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

        if args.command == "task1-cleanup":
            cleanup_stdout = _run_task1_cleanup(
                session=session,
                password=args.password,
            )
            sys.stdout.buffer.write(cleanup_stdout)
            exit_code = 0
            return exit_code

        assert args.command == "proof"
        if args.fresh:
            _upload_confirm_file(
                session=session,
                confirm_file=args.confirm_file,
                reporter=reporter,
            )
        with activate_task1_proof_context(task1_context):
            _print_expected_service_urls(
                reporter,
                _resolve_expected_service_url_links(args.env_file),
            )
        task1_binding = None
        if task1_context is not None:
            if archive_evidence is None:
                raise RepositoryArchiveError("Task 1 remote proof archive evidence is absent")
            task1_binding = Task1RemoteProofBinding(
                proof_commit=archive_evidence.commit_sha,
                context_sha256=hashlib.sha256(task1_context.to_bytes()).hexdigest(),
                uploaded_env_sha256=task1_context.uploaded_env_sha256,
                archive_sha256=archive_evidence.archive_sha256,
            )
            session.initialize_task1_receipt(task1_binding, password=args.password)
        receipt_bytes = session.run_proof(
            password=args.password,
            fresh=args.fresh,
            confirm_file=args.confirm_file,
            strict_idempotency=args.strict_idempotency,
            task1_binding=task1_binding,
        )
        if receipt_bytes is not None:
            sys.stdout.buffer.write(receipt_bytes)
        exit_code = 0
        return exit_code
    except (OSError, RemoteCommandFailure, RuntimeError, StateValidationError, ValueError) as error:
        print(_redact_runtime_message(str(error), password=args.password), file=sys.stderr)
        return 1
    finally:
        transport.close()
        reporter.progress("closed SSH connection")
        reporter.finish(args.command, exit_code=exit_code, started=started)


def _add_remote_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "positional_env_file",
        nargs="?",
        type=Path,
        metavar="ENV_FILE",
        help=(
            "install env file path; equivalent to --env-file and can provide VPS_HOST "
            "and VPS_ROOT_PASSWORD"
        ),
    )
    parser.add_argument("--host", help="target host or IP address")
    parser.add_argument("--password", help="target user password")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the target password from bounded UTF-8 stdin instead of argv",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="stream remote stdout/stderr live with command labels",
    )
    parser.add_argument(
        "--quiet-remote-output",
        dest="verbose",
        action="store_false",
        help="suppress live remote stdout/stderr stream lines",
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
        action=_EnvFileAction,
        help="install env file relative to the repo root (default: .install.env)",
    )
    parser.add_argument(
        "--task1-proof-context",
        type=Path,
        help="validated external Task 1 proof context paired with the upload-only env file",
    )
    parser.add_argument(
        "--deploy-commit",
        default="HEAD",
        help="commit to archive for remote deployment (default: HEAD)",
    )
    parser.set_defaults(_env_file_flag_provided=False)


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
    _normalize_remote_env_file(parser, args)
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


def _validate_task1_proof_context(
    args: argparse.Namespace,
) -> Task1ProofContextV1 | None:
    command = args.command
    if command not in {"install", "modify", "proof", "inspect-state", "task1-cleanup"}:
        return None
    if (
        command == "inspect-state"
        and args.task1_proof_context is None
        and not args.env_file.exists()
    ):
        return None
    raw_env = parse_env_file(args.env_file)
    return validate_task1_proof_context_argument(raw_env, args.task1_proof_context)


def _validate_runtime_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.password_stdin:
        if args.password:
            parser.error("--password and --password-stdin are mutually exclusive.")
        raw_password = sys.stdin.buffer.read(65537)
        if len(raw_password) > 65536:
            parser.error("stdin password exceeds the bounded transport limit.")
        try:
            args.password = raw_password.decode("utf-8").removesuffix("\n")
        except UnicodeDecodeError:
            parser.error("stdin password must be valid UTF-8.")
        if "\n" in args.password or "\r" in args.password:
            parser.error("stdin password must contain exactly one line.")
    _hydrate_remote_connection_args(parser, args)
    if not getattr(args, "host", None):
        parser.error("--host is required or VPS_HOST must be set in the selected env file.")
    if not getattr(args, "password", None):
        parser.error(
            "--password is required for remote SSH authentication or VPS_ROOT_PASSWORD "
            "must be set in the selected env file."
        )


class _EnvFileAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[str] | None,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, values)
        setattr(namespace, "_env_file_flag_provided", True)


def _normalize_remote_env_file(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    positional_env_file = getattr(args, "positional_env_file", None)
    flag_env_file = getattr(args, "env_file", Path(".install.env"))
    flag_provided = getattr(args, "_env_file_flag_provided", False)
    if positional_env_file is not None and flag_provided:
        if _normalized_cli_path(positional_env_file) != _normalized_cli_path(flag_env_file):
            parser.error(
                "positional env file and --env-file refer to different paths; provide only one."
            )
    if positional_env_file is not None:
        args.env_file = positional_env_file


def _hydrate_remote_connection_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    needs_host = not getattr(args, "host", None)
    needs_password = not getattr(args, "password", None)
    if not needs_host and not needs_password:
        return
    try:
        values = parse_env_file(args.env_file).values
    except FileNotFoundError:
        parser.error(f"install env file does not exist: {args.env_file}")
    except OSError as error:
        parser.error(_redact_runtime_message(str(error), password=getattr(args, "password", None)))
    except StateValidationError as error:
        parser.error(_redact_runtime_message(str(error), password=getattr(args, "password", None)))

    if needs_host:
        env_host = _configured_env_value(values, "VPS_HOST")
        if env_host is not None:
            args.host = env_host
    if needs_password:
        env_password = _configured_env_value(values, "VPS_ROOT_PASSWORD")
        if env_password is not None:
            args.password = env_password


def _configured_env_value(values: dict[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_cli_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.resolve(strict=False)


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
) -> RepositoryArchiveEvidence:
    started = time.monotonic()
    reporter.progress("creating repo archive for upload")
    with tempfile.TemporaryDirectory(prefix="dokploy-wizard-remote-") as temp_dir:
        archive_path = Path(temp_dir) / "repo.tar.gz"
        evidence = _create_repo_archive(
            repo_root=_repo_root(),
            destination=archive_path,
            deploy_commit=args.deploy_commit,
        )
        manifest_path = Path(temp_dir) / "release-manifest.json"
        write_release_manifest(manifest_path, evidence.manifest)
        bootstrap_path = Path(temp_dir) / "release-activation-bootstrap.py"
        descriptor = os.open(bootstrap_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "wb") as bootstrap_file:
            bootstrap_file.write(evidence.bootstrap_bytes)
            bootstrap_file.flush()
            os.fsync(bootstrap_file.fileno())
        reporter.progress(
            "uploading repo archive and install env file "
            f"to {session.remote_root} (env contents redacted)"
        )
        session.upload_bundle(
            repo_archive=archive_path,
            release_manifest=manifest_path,
            activation_bootstrap=bootstrap_path,
            bootstrap_sha256=evidence.bootstrap_sha256,
            install_env_file=args.env_file,
            task1_proof_context=args.task1_proof_context,
        )
    elapsed = time.monotonic() - started
    reporter.progress(f"uploaded remote bundle ({elapsed:.1f}s)")
    return evidence


def _extract_remote_bundle(
    *,
    session: RemoteTransportSession,
    archive_evidence: RepositoryArchiveEvidence,
    password: str | None,
) -> None:
    session.activate_release(
        archive_evidence.archive_sha256,
        archive_evidence.bootstrap_sha256,
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


def _build_install_command(session: RemoteTransportSession) -> str:
    arguments = [
        "./bin/dokploy-wizard",
        "install",
        "--env-file",
        session.remote_install_env_path,
        "--state-dir",
        session.remote_state_dir,
        "--non-interactive",
    ]
    arguments.extend(_task1_proof_context_arguments(session))
    return _with_unbuffered_python(_shell_join(arguments))


def _build_modify_command(
    session: RemoteTransportSession,
    *,
    capture_upgrade_observations: bool,
) -> str:
    arguments = [
        "./bin/dokploy-wizard",
        "modify",
        "--env-file",
        session.remote_install_env_path,
        "--state-dir",
        session.remote_state_dir,
        "--non-interactive",
    ]
    if capture_upgrade_observations:
        arguments.append("--task18-force-model-sync-upgrade")
    arguments.extend(_task1_proof_context_arguments(session))
    return _with_unbuffered_python(_shell_join(arguments))


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
    return _with_unbuffered_python(_shell_join(command))


def _build_inspect_state_command(session: RemoteTransportSession) -> str:
    arguments = [
        "./bin/dokploy-wizard",
        "inspect-state",
        "--env-file",
        session.remote_install_env_path,
        "--state-dir",
        session.remote_state_dir,
    ]
    arguments.extend(_task1_proof_context_arguments(session))
    return _with_unbuffered_python(_shell_join(arguments))


def _task1_proof_context_arguments(session: RemoteTransportSession) -> list[str]:
    if session.remote_task1_proof_context_path is None:
        return []
    return ["--task1-proof-context", session.remote_task1_proof_context_path]


def _run_remote_command(
    *,
    session: RemoteTransportSession,
    subcommand: str,
    command: str,
    password: str | None,
) -> None:
    session.run_command(subcommand=subcommand, command=command, password=password)


def _run_task1_cleanup(*, session: RemoteTransportSession, password: str) -> bytes:
    try:
        output = session.capture_command(
            subcommand="task1-cloudflare-cleanup",
            command=session.build_task1_cloudflare_cleanup_command(),
            limits=TASK1_CLEANUP_CAPTURE_LIMITS,
            password=password,
        )
    except RemoteCapturedCommandFailure as error:
        if error.reason == "nonzero status" and _TASK1_CLEANUP_JOURNAL_ABSENT in error.stderr:
            raise RuntimeError("Task 1 Cloudflare cleanup journal is absent") from None
        raise
    return output.stdout


def capture_remote_output(
    transport: ParamikoRemoteTransport,
    command: str,
    *,
    timeout_seconds: int,
    stdin_bytes: bytes | None = None,
) -> str:
    """Run one read-only proof probe and return stdout with a bounded wait."""
    if stdin_bytes is not None and len(stdin_bytes) > 65536:
        raise RuntimeError("remote proof input exceeds the bounded transport limit")
    stdin, stdout, stderr = transport.client.exec_command(command, timeout=timeout_seconds)
    if stdin_bytes is not None:
        if stdin is None:
            raise RuntimeError("remote proof transport does not provide stdin")
        try:
            written = stdin.write(stdin_bytes)
            if written is not None and written != len(stdin_bytes):
                raise RuntimeError("remote proof transport accepted only partial stdin")
            stdin.flush()
            stdin.close()
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeError("remote proof stdin delivery failed") from error
    started = time.monotonic()
    while not stdout.channel.exit_status_ready():
        if time.monotonic() - started > timeout_seconds:
            stdout.channel.close()
            raise RuntimeError("remote proof probe timed out")
        time.sleep(0.05)
    status = stdout.channel.recv_exit_status()
    raw_output = stdout.read(2 * 1024 * 1024 + 1)
    if not isinstance(raw_output, bytes):
        raise RuntimeError("remote proof probe returned non-byte output")
    if len(raw_output) > 2 * 1024 * 1024:
        raise RuntimeError("remote proof probe output exceeds the bounded transport limit")
    try:
        output = raw_output.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("remote proof probe returned invalid UTF-8") from error
    if status != 0:
        stderr.read(2 * 1024 * 1024 + 1)
        raise RuntimeError("remote proof probe failed")
    return output


def _resolve_expected_service_url_links(env_file: Path) -> tuple[tuple[str, str], ...]:
    desired_state = resolve_desired_state(parse_env_file(env_file))
    links: list[tuple[str, str]] = [("Dokploy", _user_facing_url(desired_state.dokploy_url))]
    for hostname_key, label in (
        ("nextcloud", "Nextcloud"),
        ("coder", "Coder"),
        ("my-farm-advisor", "My Farm Advisor/Farm"),
    ):
        hostname = desired_state.hostnames.get(hostname_key)
        if hostname:
            links.append((label, _user_facing_url(f"https://{hostname}")))
    return tuple(links)


def _user_facing_url(url: str) -> str:
    return f"{url.rstrip('/')}/"


def _print_expected_service_urls(
    reporter: "_RemoteProgressReporter",
    links: tuple[tuple[str, str], ...],
) -> None:
    reporter.progress("expected service URLs:")
    for label, url in links:
        reporter.progress(f"  {label}: {url}")


def _remote_path(remote_root: str, path: Path) -> str:
    if path.is_absolute():
        return path.as_posix()
    return posixpath.join(remote_root.rstrip("/") or "/", path.as_posix())


def _shell_join(arguments: Sequence[str]) -> str:
    return " ".join(shlex.quote(argument) for argument in arguments)


def _with_unbuffered_python(command: str) -> str:
    return f"PYTHONUNBUFFERED=1 {command}"


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


def _create_repo_archive(
    *, repo_root: Path, destination: Path, deploy_commit: str = "HEAD"
) -> RepositoryArchiveEvidence:
    try:
        return create_commit_archive(
            repo_root=repo_root,
            deploy_commit=deploy_commit,
            destination=destination,
        )
    except ReleaseError as error:
        raise RepositoryArchiveError(str(error)) from error


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
