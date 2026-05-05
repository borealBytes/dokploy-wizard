from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
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


def test_proof_runs_install_rerun_inspect_in_order(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    transport = make_fake_transport()
    session = _build_session(remote_transport_subject, transport=transport)

    session.run_proof()

    assert [subcommand for subcommand, _command in transport.commands] == [
        "install",
        "install-rerun-noop",
        "inspect-state",
    ]
    assert [command for _subcommand, command in transport.commands] == [
        (
            "./bin/dokploy-wizard install --env-file /root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive"
        ),
        (
            "./bin/dokploy-wizard install --env-file /root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive"
        ),
        (
            "./bin/dokploy-wizard inspect-state --env-file /root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state"
        ),
    ]


def test_redacts_password_from_failures(
    remote_transport_subject: ModuleType,
    make_fake_transport: Any,
) -> None:
    secret = "SuperSecretPassword123!"
    transport = make_fake_transport(failures={"install": f"remote stderr leaked {secret}"})
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
        "uninstall-destroy-data",
        "install",
        "install-rerun-noop",
        "inspect-state",
    ]
    assert [command for _subcommand, command in transport.commands] == [
        (
            "./bin/dokploy-wizard uninstall --state-dir /root/dokploy-wizard/state "
            "--destroy-data --non-interactive --confirm-file "
            "/root/dokploy-wizard/fixtures/destroy.confirm"
        ),
        (
            "./bin/dokploy-wizard install --env-file /root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive"
        ),
        (
            "./bin/dokploy-wizard install --env-file /root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state --non-interactive"
        ),
        (
            "./bin/dokploy-wizard inspect-state --env-file /root/dokploy-wizard/.install.env "
            "--state-dir /root/dokploy-wizard/state"
        ),
    ]
