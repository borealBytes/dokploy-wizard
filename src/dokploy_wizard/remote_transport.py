from __future__ import annotations

import posixpath
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import paramiko  # type: ignore[import-untyped]


def _redact_secret(value: str, password: str | None) -> str:
    if password is None or password == "":
        return value
    return value.replace(password, "<redacted>")


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
    def __init__(self, transport: RemoteTransport, remote_root: str) -> None:
        self.transport = transport
        self.remote_root = remote_root.rstrip("/") or "/"
        self.remote_archive_path = posixpath.join(self.remote_root, "repo.tar.gz")
        self.remote_install_env_path = posixpath.join(self.remote_root, ".install.env")
        self.remote_state_dir = posixpath.join(self.remote_root, "state")

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
    ) -> None:
        commands: list[tuple[str, str]] = []
        if fresh:
            if confirm_file is None:
                raise ValueError("confirm_file is required when fresh=True")
            remote_confirm_path = self._remote_path(confirm_file)
            commands.append(
                (
                    "uninstall-destroy-data",
                    " ".join(
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
                    ),
                )
            )

        commands.extend(
            [
                (
                    "install",
                    " ".join(
                        [
                            "./bin/dokploy-wizard",
                            "install",
                            "--env-file",
                            self.remote_install_env_path,
                            "--state-dir",
                            self.remote_state_dir,
                            "--non-interactive",
                        ]
                    ),
                ),
                (
                    "install-rerun-noop",
                    " ".join(
                        [
                            "./bin/dokploy-wizard",
                            "install",
                            "--env-file",
                            self.remote_install_env_path,
                            "--state-dir",
                            self.remote_state_dir,
                            "--non-interactive",
                        ]
                    ),
                ),
                (
                    "inspect-state",
                    " ".join(
                        [
                            "./bin/dokploy-wizard",
                            "inspect-state",
                            "--env-file",
                            self.remote_install_env_path,
                            "--state-dir",
                            self.remote_state_dir,
                        ]
                    ),
                ),
            ]
        )

        for subcommand, command in commands:
            try:
                self.transport.run(subcommand, command)
            except RemoteCommandFailure:
                raise
            except Exception as error:
                raise RemoteCommandFailure(
                    subcommand=subcommand,
                    error=error,
                    password=password,
                ) from error

    def _remote_path(self, path: Path) -> str:
        remote_path = path.as_posix()
        if path.is_absolute():
            return remote_path
        return posixpath.join(self.remote_root, remote_path)


class ParamikoRemoteTransport:
    def __init__(self, client: "paramiko.SSHClient", remote_root: str) -> None:
        self.client = client
        self.remote_root = remote_root.rstrip("/") or "/"

    @classmethod
    def connect(
        cls,
        *,
        hostname: str,
        username: str,
        password: str,
        remote_root: str,
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
        return cls(client=client, remote_root=remote_root)

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
        del subcommand
        self._exec(command, in_remote_root=True)

    def close(self) -> None:
        self.client.close()

    def _exec(self, command: str, *, in_remote_root: bool = False) -> None:
        remote_command = command
        if in_remote_root:
            remote_command = f"cd {shlex.quote(self.remote_root)} && {command}"
        _stdin, stdout, stderr = self.client.exec_command(remote_command)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            return

        stderr_text = stderr.read().decode("utf-8", errors="replace").strip()
        stdout_text = stdout.read().decode("utf-8", errors="replace").strip()
        details = stderr_text or stdout_text or f"remote command exited with status {exit_status}"
        raise RuntimeError(details)
