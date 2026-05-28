from __future__ import annotations

import posixpath
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from dokploy_wizard.verification import redact_text

if TYPE_CHECKING:
    import paramiko  # type: ignore[import-untyped]


ProgressCallback = Callable[[str], None]
RemoteOutputCallback = Callable[[str, str, str], None]
REMOTE_OUTPUT_HEARTBEAT_INTERVAL_SECONDS = 30.0


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


class RemoteTransport(Protocol):
    def ensure_dir(self, remote_path: str) -> None: ...

    def upload(self, local_path: Path, remote_path: str) -> None: ...

    def chmod(self, remote_path: str, mode: int) -> None: ...

    def run(self, subcommand: str, command: str) -> None: ...


class RemoteTransportSession:
    def __init__(
        self,
        transport: RemoteTransport,
        remote_root: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.transport = transport
        self.remote_root = remote_root.rstrip("/") or "/"
        self.remote_archive_path = posixpath.join(self.remote_root, "repo.tar.gz")
        self.remote_install_env_path = posixpath.join(self.remote_root, ".install.env")
        self.remote_state_dir = posixpath.join(self.remote_root, "state")
        self.progress_callback = progress_callback

    def upload_bundle(self, repo_archive: Path, install_env_file: Path) -> None:
        self.transport.ensure_dir(self.remote_root)
        self.transport.upload(repo_archive, self.remote_archive_path)
        self.transport.upload(install_env_file, self.remote_install_env_path)
        self.transport.chmod(self.remote_install_env_path, 0o600)

    def run_proof(
        self,
        password: str | None = None,
        *,
        fresh: bool = False,
        confirm_file: Path | None = None,
        strict_idempotency: bool = False,
    ) -> None:
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

        for subcommand, command in commands:
            self.run_command(subcommand=subcommand, command=command, password=password)

    def run_command(
        self,
        *,
        subcommand: str,
        command: str,
        password: str | None = None,
    ) -> None:
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

    def _emit_progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)

    def _build_install_command(self) -> str:
        return self._with_unbuffered_python(
            self._shell_join(
                [
                    "./bin/dokploy-wizard",
                    "install",
                    "--env-file",
                    self.remote_install_env_path,
                    "--state-dir",
                    self.remote_state_dir,
                    "--non-interactive",
                ]
            )
        )

    def _build_verify_services_command(self) -> str:
        return " ".join(
            [
                "PYTHONUNBUFFERED=1",
                "PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH}",
                self._shell_join(
                    [
                        "python3",
                        "-m",
                        "dokploy_wizard.service_verification_runner",
                        "--env-file",
                        self.remote_install_env_path,
                        "--state-dir",
                        self.remote_state_dir,
                    ]
                ),
            ]
        )

    def _build_inspect_state_command(self) -> str:
        return self._with_unbuffered_python(
            self._shell_join(
                [
                    "./bin/dokploy-wizard",
                    "inspect-state",
                    "--env-file",
                    self.remote_install_env_path,
                    "--state-dir",
                    self.remote_state_dir,
                ]
            )
        )

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
