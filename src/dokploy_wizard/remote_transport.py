from __future__ import annotations

import posixpath
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    Task1RemoteProofBinding,
    Task1RemoteProofStage,
)
from dokploy_wizard.verification import redact_text

if TYPE_CHECKING:
    import paramiko  # type: ignore[import-untyped]


ProgressCallback = Callable[[str], None]
RemoteOutputCallback = Callable[[str, str, str], None]
REMOTE_OUTPUT_HEARTBEAT_INTERVAL_SECONDS = 30.0
REMOTE_CAPTURE_READ_SIZE: Final[int] = 4096


@dataclass(frozen=True, slots=True)
class RemoteCommandCaptureLimits:
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("remote capture timeout must be positive")
        if self.max_stdout_bytes <= 0 or self.max_stderr_bytes <= 0:
            raise ValueError("remote capture output limits must be positive")


TASK1_RECEIPT_CAPTURE_LIMITS: Final = RemoteCommandCaptureLimits(
    timeout_seconds=30.0,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=16 * 1024,
)


@dataclass(frozen=True, slots=True)
class RemoteCommandOutput:
    stdout: bytes
    stderr: bytes


def _redact_secret(value: str, password: str | None) -> str:
    if password is not None and password != "":
        value = value.replace(password, "<redacted>")
    return redact_text(value)


class RemoteCommandFailure(RuntimeError):
    """Raised when a remote lifecycle command fails."""

    def __init__(
        self,
        *,
        subcommand: str,
        error: BaseException,
        password: str | None = None,
    ) -> None:
        details = _redact_secret(str(error), password)
        super().__init__(f"remote command failed for {subcommand}: {details}")
        self.subcommand = subcommand


class RemoteCapturedCommandFailure(RemoteCommandFailure):
    """Raised when a bounded captured command cannot return a complete result."""

    def __init__(
        self,
        *,
        subcommand: str,
        reason: Literal[
            "timed out",
            "stdout limit exceeded",
            "stderr limit exceeded",
            "nonzero status",
            "invalid stream data",
        ],
        stderr: bytes,
        exit_status: int | None = None,
    ) -> None:
        self.subcommand = subcommand
        self.reason = reason
        self.stderr = stderr
        self.exit_status = exit_status
        RuntimeError.__init__(
            self,
            f"remote captured command failed for {subcommand}: {reason}",
        )


class RemoteTransport(Protocol):
    def ensure_dir(self, remote_path: str) -> None: ...

    def upload(self, local_path: Path, remote_path: str) -> None: ...

    def chmod(self, remote_path: str, mode: int) -> None: ...

    def run(self, subcommand: str, command: str) -> None: ...

    def capture(
        self,
        subcommand: str,
        command: str,
        limits: RemoteCommandCaptureLimits,
    ) -> RemoteCommandOutput: ...


class RemoteTransportSession:
    def __init__(
        self,
        transport: RemoteTransport,
        remote_root: str,
        *,
        task1_proof_context: Path | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.transport = transport
        self.remote_root = remote_root.rstrip("/") or "/"
        self.remote_archive_path = posixpath.join(self.remote_root, "repo.tar.gz")
        self.remote_release_manifest_path = posixpath.join(
            self.remote_root, "release-manifest.json"
        )
        self.remote_activation_bootstrap_path = posixpath.join(
            self.remote_root, "release-activation-bootstrap.py"
        )
        self.remote_releases_root = posixpath.join(self.remote_root, "releases")
        self.remote_active_release_path = posixpath.join(self.remote_root, "current")
        self._release_is_active = False
        self.remote_install_env_path = posixpath.join(self.remote_root, ".install.env")
        self.remote_task1_proof_context_path = (
            None
            if task1_proof_context is None
            else posixpath.join(self.remote_root, "task1-proof-context.json")
        )
        self.remote_state_dir = posixpath.join(self.remote_root, "state")
        self.progress_callback = progress_callback

    def upload_bundle(
        self,
        repo_archive: Path,
        release_manifest: Path,
        activation_bootstrap: Path,
        bootstrap_sha256: str,
        install_env_file: Path,
        task1_proof_context: Path | None = None,
    ) -> None:
        self.transport.ensure_dir(self.remote_root)
        self.transport.upload(repo_archive, self.remote_archive_path)
        self.transport.upload(release_manifest, self.remote_release_manifest_path)
        if len(bootstrap_sha256) != 64:
            raise ValueError("release bootstrap identity is invalid")
        self.transport.upload(activation_bootstrap, self.remote_activation_bootstrap_path)
        self.transport.chmod(self.remote_activation_bootstrap_path, 0o700)
        self.transport.upload(install_env_file, self.remote_install_env_path)
        self.transport.chmod(self.remote_install_env_path, 0o600)
        if task1_proof_context is not None:
            if self.remote_task1_proof_context_path is None:
                raise ValueError("Task 1 proof context upload was not configured for this session")
            self.transport.upload(task1_proof_context, self.remote_task1_proof_context_path)
            self.transport.chmod(self.remote_task1_proof_context_path, 0o600)

    def activate_release(
        self, archive_sha256: str, bootstrap_sha256: str, password: str | None = None
    ) -> None:
        """Extract and select one content-addressed release before lifecycle commands."""

        if len(archive_sha256) != 64 or set(archive_sha256) - set("0123456789abcdef"):
            raise ValueError("release archive identity is invalid")
        self.run_command(
            subcommand="activate-release",
            command=self._build_release_activation_command(archive_sha256, bootstrap_sha256),
            password=password,
        )
        self._release_is_active = True

    def run_proof(
        self,
        password: str | None = None,
        *,
        fresh: bool = False,
        confirm_file: Path | None = None,
        strict_idempotency: bool = False,
        task1_binding: Task1RemoteProofBinding | None = None,
    ) -> bytes | None:
        if task1_binding is not None and strict_idempotency:
            raise ValueError("Task 1 remote receipts do not support strict idempotency")
        commands: list[tuple[str, str]] = []
        if fresh:
            if confirm_file is None:
                raise ValueError("confirm_file is required when fresh=True")
            remote_confirm_path = self._remote_path(confirm_file)
            commands.append(
                (
                    "mutate-uninstall-destroy-data",
                    self._build_uninstall_destroy_command(remote_confirm_path),
                )
            )

        commands.extend(
            [
                ("mutate-install", self._build_install_command()),
                ("verify-services", self._build_verify_services_command()),
            ]
        )

        if strict_idempotency:
            commands.append(("assert-strict-idempotency", self._build_install_command()))

        commands.append(("inspect-state", self._build_inspect_state_command()))

        task1_stages = {
            "mutate-install": Task1RemoteProofStage.INSTALL,
            "verify-services": Task1RemoteProofStage.VERIFY,
            "inspect-state": Task1RemoteProofStage.INSPECT,
        }
        for subcommand, command in commands:
            if task1_binding is not None and subcommand in task1_stages:
                command = self._with_receipt_advance(
                    command, task1_binding.context_sha256, task1_stages[subcommand]
                )
            self.run_command(subcommand=subcommand, command=command, password=password)
        if task1_binding is None:
            return None
        output = self.capture_command(
            subcommand="collect-task1-receipt",
            command=self._build_receipt_collect_command(task1_binding.context_sha256),
            limits=TASK1_RECEIPT_CAPTURE_LIMITS,
            password=password,
        )
        return output.stdout

    def initialize_task1_receipt(
        self, binding: Task1RemoteProofBinding, password: str | None = None
    ) -> None:
        """Create the remote archive/upload receipt before lifecycle mutation."""
        if self.remote_task1_proof_context_path is None:
            raise ValueError("Task 1 remote receipt requires a proof context")
        arguments = [
            "python3",
            "-m",
            "dokploy_wizard.proof.model_sync_task1_remote_receipt",
            "begin",
            "--state-dir",
            self.remote_state_dir,
            "--archive",
            self.remote_archive_path,
            "--env-file",
            self.remote_install_env_path,
            "--context",
            self.remote_task1_proof_context_path,
            "--proof-commit",
            binding.proof_commit,
            "--archive-sha256",
            binding.archive_sha256,
        ]
        self.run_command(
            subcommand="initialize-task1-receipt",
            command=self._python_module_command(arguments),
            password=password,
        )

    def run_command(
        self,
        *,
        subcommand: str,
        command: str,
        password: str | None = None,
    ) -> None:
        if self._release_is_active and subcommand != "activate-release":
            command = f"cd {shlex.quote(self.remote_active_release_path)} && {command}"
        self._emit_progress(f"starting remote command: {subcommand}")
        started = time.monotonic()
        try:
            self.transport.run(subcommand, command)
        except RemoteCommandFailure:
            elapsed = time.monotonic() - started
            self._emit_progress(f"failed remote command: {subcommand} ({elapsed:.1f}s)")
            raise
        except Exception as error:
            elapsed = time.monotonic() - started
            self._emit_progress(f"failed remote command: {subcommand} ({elapsed:.1f}s)")
            raise RemoteCommandFailure(
                subcommand=subcommand,
                error=error,
                password=password,
            ) from error
        elapsed = time.monotonic() - started
        self._emit_progress(f"completed remote command: {subcommand} ({elapsed:.1f}s)")

    def capture_command(
        self,
        *,
        subcommand: str,
        command: str,
        limits: RemoteCommandCaptureLimits,
        password: str | None = None,
    ) -> RemoteCommandOutput:
        self._emit_progress(f"starting remote command: {subcommand}")
        started = time.monotonic()
        try:
            output = self.transport.capture(subcommand, command, limits)
        except RemoteCommandFailure:
            elapsed = time.monotonic() - started
            self._emit_progress(f"failed remote command: {subcommand} ({elapsed:.1f}s)")
            raise
        except Exception as error:
            elapsed = time.monotonic() - started
            self._emit_progress(f"failed remote command: {subcommand} ({elapsed:.1f}s)")
            raise RemoteCommandFailure(
                subcommand=subcommand,
                error=error,
                password=password,
            ) from error
        elapsed = time.monotonic() - started
        self._emit_progress(f"completed remote command: {subcommand} ({elapsed:.1f}s)")
        return output

    def _emit_progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)

    def _build_install_command(self) -> str:
        arguments = [
            "./bin/dokploy-wizard",
            "install",
            "--env-file",
            self.remote_install_env_path,
            "--state-dir",
            self.remote_state_dir,
            "--non-interactive",
        ]
        arguments.extend(self._task1_proof_context_arguments())
        return self._with_unbuffered_python(self._shell_join(arguments))

    def _build_release_activation_command(self, archive_sha256: str, bootstrap_sha256: str) -> str:
        activate = self._shell_join(
            [
                "python3",
                self.remote_activation_bootstrap_path,
                "--archive",
                self.remote_archive_path,
                "--manifest",
                self.remote_release_manifest_path,
                "--releases-root",
                self.remote_releases_root,
                "--bootstrap-sha256",
                bootstrap_sha256,
                "--active-link",
                self.remote_active_release_path,
            ]
        )
        bootstrap_path = shlex.quote(self.remote_activation_bootstrap_path)
        bootstrap_digest = shlex.quote(bootstrap_sha256)
        verify = f"test $(sha256sum {bootstrap_path} | awk '{{print $1}}') = {bootstrap_digest}"
        return f"{verify} && {activate}"

    def _build_verify_services_command(self) -> str:
        arguments = [
            "python3",
            "-m",
            "dokploy_wizard.service_verification_runner",
            "--env-file",
            self.remote_install_env_path,
            "--state-dir",
            self.remote_state_dir,
        ]
        arguments.extend(self._task1_proof_context_arguments())
        return " ".join(
            [
                "PYTHONUNBUFFERED=1",
                "PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH}",
                self._shell_join(arguments),
            ]
        )

    def _build_inspect_state_command(self) -> str:
        arguments = [
            "./bin/dokploy-wizard",
            "inspect-state",
            "--env-file",
            self.remote_install_env_path,
            "--state-dir",
            self.remote_state_dir,
        ]
        arguments.extend(self._task1_proof_context_arguments())
        return self._with_unbuffered_python(self._shell_join(arguments))

    def build_task1_cloudflare_cleanup_command(self) -> str:
        if self.remote_task1_proof_context_path is None:
            raise ValueError("Task 1 Cloudflare cleanup requires a proof context")
        return " ".join(
            [
                "PYTHONUNBUFFERED=1",
                "PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH}",
                self._shell_join(
                    [
                        "python3",
                        "-m",
                        "dokploy_wizard.proof.model_sync_task1_cloudflare_journal",
                        "--env-file",
                        self.remote_install_env_path,
                        "--state-dir",
                        self.remote_state_dir,
                        "--task1-proof-context",
                        self.remote_task1_proof_context_path,
                    ]
                ),
            ]
        )

    def _with_receipt_advance(
        self, command: str, context_sha256: str, stage: Task1RemoteProofStage
    ) -> str:
        advance = self._python_module_command(
            [
                "python3",
                "-m",
                "dokploy_wizard.proof.model_sync_task1_remote_receipt",
                "advance",
                "--state-dir",
                self.remote_state_dir,
                "--context-sha256",
                context_sha256,
                "--stage",
                str(stage),
            ]
        )
        return f"{command} && {advance}"

    def _build_receipt_collect_command(self, context_sha256: str) -> str:
        return self._python_module_command(
            [
                "python3",
                "-m",
                "dokploy_wizard.proof.model_sync_task1_remote_receipt",
                "collect",
                "--state-dir",
                self.remote_state_dir,
                "--context-sha256",
                context_sha256,
            ]
        )

    def _python_module_command(self, arguments: list[str]) -> str:
        return " ".join(
            [
                "PYTHONUNBUFFERED=1",
                "PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH}",
                self._shell_join(arguments),
            ]
        )

    def _task1_proof_context_arguments(self) -> list[str]:
        if self.remote_task1_proof_context_path is None:
            return []
        return ["--task1-proof-context", self.remote_task1_proof_context_path]

    def _build_uninstall_destroy_command(self, remote_confirm_path: str) -> str:
        return self._with_unbuffered_python(
            self._shell_join(
                [
                    "./bin/dokploy-wizard",
                    "uninstall",
                    "--state-dir",
                    self.remote_state_dir,
                    "--destroy-data",
                    "--non-interactive",
                    "--confirm-file",
                    remote_confirm_path,
                ]
            )
        )

    def _shell_join(self, arguments: list[str]) -> str:
        return " ".join(shlex.quote(argument) for argument in arguments)

    def _with_unbuffered_python(self, command: str) -> str:
        return f"PYTHONUNBUFFERED=1 {command}"

    def _remote_path(self, path: Path) -> str:
        remote_path = path.as_posix()
        if path.is_absolute():
            return remote_path
        return posixpath.join(self.remote_root, remote_path)


class ParamikoRemoteTransport:
    def __init__(
        self,
        client: "paramiko.SSHClient",
        remote_root: str,
        *,
        verbose: bool = False,
        output_callback: RemoteOutputCallback | None = None,
        password: str | None = None,
        heartbeat_interval: float = REMOTE_OUTPUT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.client = client
        self.remote_root = remote_root.rstrip("/") or "/"
        self.verbose = verbose
        self.output_callback = output_callback
        self.password = password
        self.heartbeat_interval = heartbeat_interval

    @classmethod
    def connect(
        cls,
        *,
        hostname: str,
        username: str,
        password: str,
        remote_root: str,
        verbose: bool = False,
        output_callback: RemoteOutputCallback | None = None,
        port: int = 22,
        timeout: float = 10,
    ) -> "ParamikoRemoteTransport":
        try:
            import paramiko
        except ModuleNotFoundError as error:  # pragma: no cover - depends on env setup
            raise RuntimeError("paramiko is required for remote transport") from error

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=hostname,
                port=port,
                username=username,
                password=password,
                allow_agent=False,
                look_for_keys=False,
                timeout=timeout,
            )
        except paramiko.SSHException as error:
            client.close()
            details = _redact_secret(str(error), password)
            raise RuntimeError(
                f"SSH connection failed for {username}@{hostname}:{port}: {details}"
            ) from error
        except Exception:
            client.close()
            raise
        return cls(
            client=client,
            remote_root=remote_root,
            verbose=verbose,
            output_callback=output_callback,
            password=password,
        )

    def ensure_dir(self, remote_path: str) -> None:
        self._exec(f"mkdir -p {shlex.quote(remote_path)}")

    def upload(self, local_path: Path, remote_path: str) -> None:
        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
        finally:
            sftp.close()

    def chmod(self, remote_path: str, mode: int) -> None:
        sftp = self.client.open_sftp()
        try:
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def run(self, subcommand: str, command: str) -> None:
        self._exec(command, in_remote_root=True, subcommand=subcommand)

    def capture(
        self,
        subcommand: str,
        command: str,
        limits: RemoteCommandCaptureLimits,
    ) -> RemoteCommandOutput:
        remote_command = f"cd {shlex.quote(self.remote_root)} && {command}"
        _stdin, stdout, _stderr = self.client.exec_command(
            remote_command,
            timeout=limits.timeout_seconds,
        )
        stdout_bytes = bytearray()
        stderr_bytes = bytearray()
        started = time.monotonic()

        def drain_available() -> bool:
            drained = False
            while stdout.channel.recv_ready():
                data = stdout.channel.recv(REMOTE_CAPTURE_READ_SIZE)
                if not data:
                    break
                if not isinstance(data, bytes):
                    stdout.channel.close()
                    raise RemoteCapturedCommandFailure(
                        subcommand=subcommand,
                        reason="invalid stream data",
                        stderr=bytes(stderr_bytes),
                    )
                if len(stdout_bytes) + len(data) > limits.max_stdout_bytes:
                    stdout.channel.close()
                    raise RemoteCapturedCommandFailure(
                        subcommand=subcommand,
                        reason="stdout limit exceeded",
                        stderr=bytes(stderr_bytes),
                    )
                stdout_bytes.extend(data)
                drained = True
            while stdout.channel.recv_stderr_ready():
                data = stdout.channel.recv_stderr(REMOTE_CAPTURE_READ_SIZE)
                if not data:
                    break
                if not isinstance(data, bytes):
                    stdout.channel.close()
                    raise RemoteCapturedCommandFailure(
                        subcommand=subcommand,
                        reason="invalid stream data",
                        stderr=bytes(stderr_bytes),
                    )
                if len(stderr_bytes) + len(data) > limits.max_stderr_bytes:
                    stdout.channel.close()
                    raise RemoteCapturedCommandFailure(
                        subcommand=subcommand,
                        reason="stderr limit exceeded",
                        stderr=bytes(stderr_bytes),
                    )
                stderr_bytes.extend(data)
                drained = True
            return drained

        while not stdout.channel.exit_status_ready():
            drained = drain_available()
            if time.monotonic() - started > limits.timeout_seconds:
                stdout.channel.close()
                raise RemoteCapturedCommandFailure(
                    subcommand=subcommand,
                    reason="timed out",
                    stderr=bytes(stderr_bytes),
                )
            if not drained:
                time.sleep(0.05)
        drain_available()
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise RemoteCapturedCommandFailure(
                subcommand=subcommand,
                reason="nonzero status",
                stderr=bytes(stderr_bytes),
                exit_status=exit_status,
            )
        return RemoteCommandOutput(stdout=bytes(stdout_bytes), stderr=bytes(stderr_bytes))

    def close(self) -> None:
        self.client.close()

    def _exec(
        self,
        command: str,
        *,
        in_remote_root: bool = False,
        subcommand: str = "remote",
    ) -> None:
        remote_command = command
        if in_remote_root:
            remote_command = f"cd {shlex.quote(self.remote_root)} && {command}"
        _stdin, stdout, stderr = self.client.exec_command(remote_command)
        if self.verbose and self.output_callback is not None:
            exit_status, stdout_text, stderr_text = self._stream_command_output(
                stdout.channel,
                subcommand=subcommand,
            )
            if exit_status == 0:
                return
            details = (
                stderr_text.strip()
                or stdout_text.strip()
                or f"remote command exited with status {exit_status}"
            )
            raise RuntimeError(details)

        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            return

        stderr_text = stderr.read().decode("utf-8", errors="replace").strip()
        stdout_text = stdout.read().decode("utf-8", errors="replace").strip()
        details = stderr_text or stdout_text or f"remote command exited with status {exit_status}"
        raise RuntimeError(details)

    def _stream_command_output(
        self,
        channel: Any,
        *,
        subcommand: str,
    ) -> tuple[int, str, str]:
        stdout_buffer = _StreamLineBuffer(
            subcommand=subcommand,
            stream_name="stdout",
            callback=self.output_callback,
            password=self.password,
        )
        stderr_buffer = _StreamLineBuffer(
            subcommand=subcommand,
            stream_name="stderr",
            callback=self.output_callback,
            password=self.password,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        started = time.monotonic()
        next_heartbeat_at = started + self.heartbeat_interval

        while not channel.exit_status_ready():
            drained = self._drain_channel(
                channel,
                stdout_buffer=stdout_buffer,
                stderr_buffer=stderr_buffer,
                stdout_parts=stdout_parts,
                stderr_parts=stderr_parts,
            )
            if drained:
                next_heartbeat_at = time.monotonic() + self.heartbeat_interval
            else:
                now = time.monotonic()
                if now >= next_heartbeat_at:
                    self._emit_heartbeat(subcommand=subcommand, elapsed=now - started)
                    next_heartbeat_at = now + self.heartbeat_interval
                time.sleep(0.1)

        self._drain_channel(
            channel,
            stdout_buffer=stdout_buffer,
            stderr_buffer=stderr_buffer,
            stdout_parts=stdout_parts,
            stderr_parts=stderr_parts,
        )
        exit_status = channel.recv_exit_status()
        stdout_buffer.flush()
        stderr_buffer.flush()
        return exit_status, "".join(stdout_parts), "".join(stderr_parts)

    def _emit_heartbeat(self, *, subcommand: str, elapsed: float) -> None:
        if self.output_callback is not None:
            self.output_callback(
                subcommand,
                "progress",
                f"still running {subcommand} ({elapsed:.0f}s elapsed, waiting for output)",
            )

    def _drain_channel(
        self,
        channel: Any,
        *,
        stdout_buffer: "_StreamLineBuffer",
        stderr_buffer: "_StreamLineBuffer",
        stdout_parts: list[str],
        stderr_parts: list[str],
    ) -> bool:
        drained = False
        while channel.recv_ready():
            data = channel.recv(4096)
            if not data:
                break
            stdout_parts.append(stdout_buffer.feed(data))
            drained = True
        while channel.recv_stderr_ready():
            data = channel.recv_stderr(4096)
            if not data:
                break
            stderr_parts.append(stderr_buffer.feed(data))
            drained = True
        return drained


class _StreamLineBuffer:
    def __init__(
        self,
        *,
        subcommand: str,
        stream_name: str,
        callback: RemoteOutputCallback | None,
        password: str | None,
    ) -> None:
        self.subcommand = subcommand
        self.stream_name = stream_name
        self.callback = callback
        self.password = password
        self._pending = ""
        self._redact_next_assignment = False

    def feed(self, data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._emit(line.rstrip("\r"))
        return text

    def flush(self) -> None:
        if self._pending:
            self._emit(self._pending.rstrip("\r"))
            self._pending = ""

    def _emit(self, line: str) -> None:
        if self.callback is not None:
            if self._redact_next_assignment and _looks_like_env_assignment(line):
                key = line.split("=", 1)[0]
                line = f"{key}=<REDACTED>"
                self._redact_next_assignment = False
            else:
                self._redact_next_assignment = False
            if line.startswith("# dokploy-wizard-env"):
                self._redact_next_assignment = True
            self.callback(
                self.subcommand,
                self.stream_name,
                _redact_secret(line, self.password),
            )


def _looks_like_env_assignment(line: str) -> bool:
    key, separator, _value = line.partition("=")
    return bool(separator and key and key.replace("_", "").isalnum())
