from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


class FakeTransport:
    def __init__(self, *, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.created_directories: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.chmod_calls: list[tuple[str, int]] = []
        self.commands: list[tuple[str, str]] = []

    def ensure_dir(self, remote_path: str) -> None:
        self.created_directories.append(remote_path)

    def upload(self, local_path: Path, remote_path: str) -> None:
        self.uploads.append((str(local_path), remote_path))

    def chmod(self, remote_path: str, mode: int) -> None:
        self.chmod_calls.append((remote_path, mode))

    def run(self, subcommand: str, command: str) -> None:
        self.commands.append((subcommand, command))
        failure = self.failures.get(subcommand)
        if failure is not None:
            raise RuntimeError(failure)


class SilentChannel:
    def __init__(self, *, running_polls: int = 3) -> None:
        self.running_polls = running_polls
        self.polls = 0

    def exit_status_ready(self) -> bool:
        self.polls += 1
        return self.polls > self.running_polls

    def recv_ready(self) -> bool:
        return False

    def recv_stderr_ready(self) -> bool:
        return False

    def recv_exit_status(self) -> int:
        return 0


@pytest.fixture
def remote_transport_subject() -> ModuleType:
    return importlib.import_module("dokploy_wizard.remote_transport")


@pytest.fixture
def repo_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(b"fake-archive")
    return archive


@pytest.fixture
def install_env_file(tmp_path: Path) -> Path:
    install_env = tmp_path / ".install.env"
    install_env.write_text("ROOT_DOMAIN=example.com\n", encoding="utf-8")
    return install_env


@pytest.fixture
def make_fake_transport() -> Any:
    def _make(*, failures: dict[str, str] | None = None) -> FakeTransport:
        return FakeTransport(failures=failures)

    return _make


def _build_session(subject: ModuleType, *, transport: FakeTransport) -> Any:
    return subject.RemoteTransportSession(
        transport=transport,
        remote_root="/root/dokploy-wizard",
    )


def _build_session_with_progress(
    subject: ModuleType,
    *,
    transport: FakeTransport,
    progress: list[str],
) -> Any:
    return subject.RemoteTransportSession(
        transport=transport,
        remote_root="/root/dokploy-wizard",
        progress_callback=progress.append,
    )


def test_upload_records_remote_path_and_env_chmod(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
    repo_archive: Path,
    install_env_file: Path,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.upload_bundle(repo_archive=repo_archive, install_env_file=install_env_file)

    assert transport.created_directories == ["/root/dokploy-wizard"]
    assert transport.uploads == [
        (str(repo_archive), "/root/dokploy-wizard/repo.tar.gz"),
        (str(install_env_file), "/root/dokploy-wizard/.install.env"),
    ]
    assert transport.chmod_calls == [("/root/dokploy-wizard/.install.env", 0o600)]


def test_paramiko_ssh_exception_is_wrapped_redacted_and_closes_client(
    remote_transport_subject: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SuperSecretPassword123!"
    clients: list[Any] = []

    class FakeSSHException(Exception):
        pass

    class FakeAutoAddPolicy:
        pass

    class FakeSSHClient:
        def __init__(self) -> None:
            self.closed = False
            clients.append(self)

        def set_missing_host_key_policy(self, _policy: Any) -> None:
            return None

        def connect(self, **_kwargs: Any) -> None:
            raise FakeSSHException(f"bad auth with password={secret}")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(
        sys.modules,
        "paramiko",
        SimpleNamespace(
            SSHClient=FakeSSHClient,
            AutoAddPolicy=FakeAutoAddPolicy,
            SSHException=FakeSSHException,
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        remote_transport_subject.ParamikoRemoteTransport.connect(
            hostname="127.0.0.1",
            username="root",
            password=secret,
            remote_root="/root/dokploy-wizard",
        )

    message = str(excinfo.value)
    assert "SSH connection failed for root@127.0.0.1:22" in message
    assert secret not in message
    assert "<REDACTED>" in message
    assert clients[0].closed is True


def test_proof_runs_install_verify_inspect_in_order(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.run_proof()

    assert [subcommand for subcommand, _command in transport.commands] == [
        "mutate-install",
        "verify-services",
        "inspect-state",
    ]
    assert [command for _subcommand, command in transport.commands] == [
        (
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard install --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive"
        ),
        (
            "PYTHONUNBUFFERED=1 PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH} python3 -m "
            "dokploy_wizard.service_verification_runner --env-file "
            "/root/dokploy-wizard/.install.env --state-dir /root/dokploy-wizard/state"
        ),
        (
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard inspect-state --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state"
        ),
    ]


def test_proof_emits_progress_for_each_remote_command(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    progress: list[str] = []
    transport = make_fake_transport()
    session = _build_session_with_progress(
        remote_transport_subject,
        transport=transport,
        progress=progress,
    )

    session.run_proof()

    assert len(progress) == 6
    assert progress[0] == "starting remote command: mutate-install"
    assert progress[1].startswith("completed remote command: mutate-install (")
    assert progress[2] == "starting remote command: verify-services"
    assert progress[3].startswith("completed remote command: verify-services (")
    assert progress[4] == "starting remote command: inspect-state"
    assert progress[5].startswith("completed remote command: inspect-state (")


def test_strict_proof_runs_second_install_after_verification(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.run_proof(strict_idempotency=True)

    assert [subcommand for subcommand, _command in transport.commands] == [
        "mutate-install",
        "verify-services",
        "assert-strict-idempotency",
        "inspect-state",
    ]
    assert transport.commands[2][1] == (
        "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard install --env-file "
        "/root/dokploy-wizard/.install.env "
        "--state-dir /root/dokploy-wizard/state --non-interactive"
    )


def test_fresh_strict_proof_runs_second_install_after_verification(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.run_proof(
        fresh=True,
        confirm_file=Path("fixtures/destroy.confirm"),
        strict_idempotency=True,
    )

    assert [subcommand for subcommand, _command in transport.commands] == [
        "mutate-uninstall-destroy-data",
        "mutate-install",
        "verify-services",
        "assert-strict-idempotency",
        "inspect-state",
    ]


def test_redacts_password_from_failures(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    secret = "SuperSecretPassword123!"
    transport = make_fake_transport(failures={"mutate-install": f"remote stderr leaked {secret}"})
    session = _build_session(remote_transport_subject, transport=transport)

    with pytest.raises(remote_transport_subject.RemoteCommandFailure) as excinfo:
        session.run_proof(password=secret)

    message = str(excinfo.value)
    assert secret not in message
    assert "<redacted>" in message


def test_remote_command_failure_reports_subcommand_without_secrets(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    secret = "SuperSecretPassword123!"
    transport = make_fake_transport(
        failures={
            "inspect-state": f"inspect-state exploded with password={secret}",
        }
    )
    session = _build_session(remote_transport_subject, transport=transport)

    with pytest.raises(remote_transport_subject.RemoteCommandFailure) as excinfo:
        session.run_proof(password=secret)

    message = str(excinfo.value)
    assert "inspect-state" in message
    assert secret not in message


def test_fresh_proof_runs_destroy_uninstall_before_proof(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.run_proof(fresh=True, confirm_file=Path("fixtures/destroy.confirm"))

    assert [subcommand for subcommand, _command in transport.commands] == [
        "mutate-uninstall-destroy-data",
        "mutate-install",
        "verify-services",
        "inspect-state",
    ]
    assert [command for _subcommand, command in transport.commands] == [
        (
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard uninstall --state-dir "
            "/root/dokploy-wizard/state "
            "--destroy-data --non-interactive --confirm-file "
            "/root/dokploy-wizard/fixtures/destroy.confirm"
        ),
        (
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard install --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive"
        ),
        (
            "PYTHONUNBUFFERED=1 PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH} python3 -m "
            "dokploy_wizard.service_verification_runner --env-file "
            "/root/dokploy-wizard/.install.env --state-dir /root/dokploy-wizard/state"
        ),
        (
            "PYTHONUNBUFFERED=1 ./bin/dokploy-wizard inspect-state --env-file "
            "/root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state"
        ),
    ]


def test_verify_services_command_uses_unquoted_pythonpath_assignment(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.run_proof()

    verify_command = dict(transport.commands)["verify-services"]
    assert verify_command.startswith(
        "PYTHONUNBUFFERED=1 PYTHONPATH=./src${PYTHONPATH:+:$PYTHONPATH} "
    )
    assert "python3 -m dokploy_wizard.service_verification_runner" in verify_command


def test_proof_lifecycle_commands_run_python_unbuffered(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.run_proof(strict_idempotency=True)

    assert all(
        command.startswith("PYTHONUNBUFFERED=1 ")
        for _subcommand, command in transport.commands
    )


def test_verify_service_failures_bubble_through_proof(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport(
        failures={
            "verify-services": (
                '{"entries":[{"detail":"OPENCLAW_VIRTUAL_KEY=<REDACTED>",'
                '"service_id":"openclaw","status":"fail"}],"status":"fail"}'
            )
        }
    )
    session = _build_session(remote_transport_subject, transport=transport)

    with pytest.raises(remote_transport_subject.RemoteCommandFailure) as excinfo:
        session.run_proof(password="SuperSecretPassword123!")

    message = str(excinfo.value)
    assert "verify-services" in message
    assert "<REDACTED>" in message


def test_remote_failures_redact_dokploy_env_payload_values(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    sentinel = "SECRET_TEST_OPENCLAW_PROVIDER_VALUE"
    transport = make_fake_transport(
        failures={
            "inspect-state": (
                "Dokploy API failed with env payload:\n"
                "# dokploy-wizard-env marker=dokploy-wizard owner=openclaw "
                "key=OPENCLAW_PROVIDER_API_KEY fingerprint=sha256:abc123\n"
                f"OPENCLAW_PROVIDER_API_KEY={sentinel}"
            )
        }
    )
    session = _build_session(remote_transport_subject, transport=transport)

    with pytest.raises(remote_transport_subject.RemoteCommandFailure) as excinfo:
        session.run_proof(password="SuperSecretPassword123!")

    message = str(excinfo.value)
    assert "inspect-state" in message
    assert sentinel not in message
    assert "OPENCLAW_PROVIDER_API_KEY=<REDACTED>" in message
    assert "fingerprint=sha256:abc123" in message


def test_verbose_stream_buffer_redacts_multiline_env_payload_and_password(
    remote_transport_subject: ModuleType,
) -> None:
    password = "SuperSecretPassword123!"
    env_secret = "SECRET_TEST_OPENCLAW_PROVIDER_VALUE"
    docker_pat = "ghp_SECRETTESTDOCKERPATVALUE"
    output: list[tuple[str, str, str]] = []
    buffer = remote_transport_subject._StreamLineBuffer(
        subcommand="mutate-install",
        stream_name="stdout",
        callback=lambda subcommand, stream_name, line: output.append(
            (subcommand, stream_name, line)
        ),
        password=password,
    )

    buffer.feed(
        b"# dokploy-wizard-env marker=dokploy-wizard owner=openclaw "
        b"key=OPENCLAW_PROVIDER_API_KEY fingerprint=sha256:abc123\n"
    )
    buffer.feed(f"OPENCLAW_PROVIDER_API_KEY={env_secret}\n".encode())
    buffer.feed(f"password={password}\nDOCKER_PAT={docker_pat}\n".encode())

    rendered = "\n".join(line for _subcommand, _stream_name, line in output)
    assert env_secret not in rendered
    assert password not in rendered
    assert docker_pat not in rendered
    assert "OPENCLAW_PROVIDER_API_KEY=<REDACTED>" in rendered
    assert "password=<REDACTED>" in rendered
    assert "DOCKER_PAT=<REDACTED>" in rendered


def test_verbose_stream_outputs_heartbeat_for_silent_remote_command(
    remote_transport_subject: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output: list[tuple[str, str, str]] = []
    monotonic_values = iter([0.0, 0.5, 1.0])
    monkeypatch.setattr(remote_transport_subject.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(remote_transport_subject.time, "sleep", lambda _seconds: None)
    transport = remote_transport_subject.ParamikoRemoteTransport(
        client=object(),
        remote_root="/root/dokploy-wizard",
        verbose=True,
        output_callback=lambda subcommand, stream_name, line: output.append(
            (subcommand, stream_name, line)
        ),
        heartbeat_interval=0.5,
    )

    exit_status, stdout_text, stderr_text = transport._stream_command_output(
        SilentChannel(running_polls=2),
        subcommand="mutate-install",
    )

    assert exit_status == 0
    assert stdout_text == ""
    assert stderr_text == ""
    assert output == [
        (
            "mutate-install",
            "progress",
            "still running mutate-install (0s elapsed, waiting for output)",
        ),
        (
            "mutate-install",
            "progress",
            "still running mutate-install (1s elapsed, waiting for output)",
        ),
    ]
