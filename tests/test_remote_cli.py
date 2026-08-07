from __future__ import annotations

import importlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from typing_extensions import Buffer

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "dokploy-wizard-remote"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def import_remote_cli_module() -> ModuleType:
    try:
        return importlib.import_module("dokploy_wizard.remote")
    except ModuleNotFoundError as exc:
        assert False, f"expected dokploy_wizard.remote module for remote CLI contract: {exc}"


class _CaptureChannel:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def close(self) -> None:
        return None

    def exit_status_ready(self) -> bool:
        return self.ready

    def recv_exit_status(self) -> int:
        return 0


class _CaptureStream(io.BytesIO):
    def __init__(self, payload: bytes = b"", *, ready: bool = True) -> None:
        super().__init__(payload)
        self.channel = _CaptureChannel(ready=ready)


class _PartialCaptureStream(_CaptureStream):
    def write(self, payload: Buffer) -> int:
        return memoryview(payload).nbytes - 1


class _CaptureClient:
    def __init__(
        self,
        *,
        partial_write: bool = False,
        output: bytes = b'{"ok":true}',
        ready: bool = True,
    ) -> None:
        self.stdin = _PartialCaptureStream() if partial_write else _CaptureStream()
        self.command = ""
        self.output = output
        self.ready = ready

    def exec_command(
        self, command: str, *, timeout: int
    ) -> tuple[_CaptureStream, _CaptureStream, _CaptureStream]:
        del timeout
        self.command = command
        return self.stdin, _CaptureStream(self.output, ready=self.ready), _CaptureStream()


class _CaptureTransport:
    def __init__(self, client: _CaptureClient) -> None:
        self.client = client


def test_remote_capture_sends_secret_payload_only_through_bounded_stdin() -> None:
    remote_cli = import_remote_cli_module()
    secret = "SECRET-STDIN-SENTINEL"
    client = _CaptureClient()
    transport = _CaptureTransport(client)

    output = remote_cli.capture_remote_output(
        transport,
        "python3 -c safe-collector",
        timeout_seconds=5,
        stdin_bytes=json.dumps({"token": secret}).encode(),
    )

    assert output == '{"ok":true}'
    assert secret not in client.command
    assert secret not in output
    assert secret not in repr(transport)


def test_remote_capture_rejects_partial_stdin_without_reflecting_secret() -> None:
    remote_cli = import_remote_cli_module()
    secret = "SECRET-PARTIAL-WRITE-SENTINEL"
    transport = _CaptureTransport(_CaptureClient(partial_write=True))

    with pytest.raises(RuntimeError) as caught:
        remote_cli.capture_remote_output(
            transport,
            "python3 -c safe-collector",
            timeout_seconds=5,
            stdin_bytes=secret.encode(),
        )

    assert secret not in str(caught.value)


@pytest.mark.parametrize("output", [b"\xff", b"x" * (2 * 1024 * 1024 + 1)])
def test_remote_capture_rejects_invalid_or_oversized_output(output: bytes) -> None:
    remote_cli = import_remote_cli_module()

    with pytest.raises(RuntimeError):
        remote_cli.capture_remote_output(
            _CaptureTransport(_CaptureClient(output=output)),
            "python3 -c safe-collector",
            timeout_seconds=5,
        )


def test_remote_capture_timeout_closes_without_exposing_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_cli = import_remote_cli_module()
    secret = "SECRET-TIMEOUT-SENTINEL"
    moments = iter((0.0, 6.0))
    monkeypatch.setattr(remote_cli.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(remote_cli.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError) as caught:
        remote_cli.capture_remote_output(
            _CaptureTransport(_CaptureClient(ready=False)),
            "python3 -c safe-collector",
            timeout_seconds=5,
            stdin_bytes=secret.encode(),
        )

    assert secret not in str(caught.value)


class _FakeRemoteTransport:
    def __init__(self, *, output_callback: Any | None) -> None:
        self.output_callback = output_callback
        self.closed = False

    def ensure_dir(self, _remote_path: str) -> None:
        return None

    def upload(self, _local_path: Path, _remote_path: str) -> None:
        return None

    def download(self, _remote_path: str, _local_path: Path) -> None:
        return None

    def remove(self, _remote_path: str) -> None:
        return None

    def chmod(self, _remote_path: str, _mode: int) -> None:
        return None

    def run(self, subcommand: str, _command: str) -> None:
        if self.output_callback is not None:
            self.output_callback(subcommand, "stdout", f"{subcommand} streamed")

    def capture(self, _subcommand: str, _command: str, _limits: Any) -> Any:
        raise AssertionError("capture should not run for ordinary remote commands")

    def close(self) -> None:
        self.closed = True


class _Task1CleanupTransport:
    def __init__(self, cleanup_stdout: bytes) -> None:
        self.cleanup_stdout = cleanup_stdout
        self.closed = False
        self.capture_calls: list[tuple[str, str]] = []

    def ensure_dir(self, _remote_path: str) -> None:
        return None

    def upload(self, _local_path: Path, _remote_path: str) -> None:
        return None

    def download(self, _remote_path: str, _local_path: Path) -> None:
        return None

    def remove(self, _remote_path: str) -> None:
        return None

    def chmod(self, _remote_path: str, _mode: int) -> None:
        return None

    def run(self, _subcommand: str, _command: str) -> None:
        return None

    def capture(self, subcommand: str, command: str, _limits: Any) -> Any:
        self.capture_calls.append((subcommand, command))
        from dokploy_wizard.remote_transport import RemoteCommandOutput

        return RemoteCommandOutput(stdout=self.cleanup_stdout, stderr=b"cleanup progress\n")

    def close(self) -> None:
        self.closed = True


def _write_remote_env(tmp_path: Path, *, packs: str) -> Path:
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "\n".join(
            [
                "ROOT_DOMAIN=openmerge.me",
                f"PACKS={packs}",
                "AI_DEFAULT_PROVIDER=openrouter",
                "AI_DEFAULT_MODEL=deepseek/deepseek-v4-flash:free",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env_file


def _patch_successful_remote_run(
    monkeypatch: pytest.MonkeyPatch,
    remote_cli: ModuleType,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> _FakeRemoteTransport:
        captured["verbose"] = kwargs["verbose"]
        return _FakeRemoteTransport(output_callback=kwargs["output_callback"])

    monkeypatch.setattr(remote_cli.ParamikoRemoteTransport, "connect", fake_connect)
    monkeypatch.setattr(remote_cli, "_upload_remote_bundle", lambda **_kwargs: None)
    monkeypatch.setattr(remote_cli, "_extract_remote_bundle", lambda **_kwargs: None)
    return captured


def test_help_lists_expected_subcommands() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli("--help")

    assert result.returncode == 0
    assert "install" in result.stdout
    assert "modify" in result.stdout
    assert "uninstall" in result.stdout
    assert "inspect-state" in result.stdout
    assert "proof" in result.stdout
    assert result.stderr == ""


def test_state_upgrade_observe_uses_context_bound_read_only_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    remote_cli = import_remote_cli_module()
    transport = _Task1CleanupTransport(b"")
    env_file = _write_remote_env(tmp_path, packs="nextcloud")
    context = tmp_path / "task1-proof-context.json"

    monkeypatch.setattr(
        remote_cli.ParamikoRemoteTransport,
        "connect",
        lambda **_kwargs: transport,
    )
    monkeypatch.setattr(remote_cli, "_validate_task1_proof_context", lambda _args: None)
    monkeypatch.setattr(remote_cli, "_upload_remote_bundle", lambda **_kwargs: None)
    monkeypatch.setattr(remote_cli, "_extract_remote_bundle", lambda **_kwargs: None)
    monkeypatch.setattr(
        remote_cli,
        "_capture_state_upgrade_observation",
        lambda **_kwargs: b'{"schema_version":1,"task1_context_active":true}\n',
    )

    exit_code = remote_cli.main(
        [
            "state-upgrade-observe",
            "--host",
            "example.com",
            "--password",
            "password-sentinel",
            "--env-file",
            str(env_file),
            "--task1-proof-context",
            str(context),
        ]
    )

    captured = capsysbinary.readouterr()

    assert exit_code == 0
    assert captured.out == b'{"schema_version":1,"task1_context_active":true}\n'
    assert b"password-sentinel" not in captured.err


def test_remote_parser_defaults_match_contract() -> None:
    remote_cli = import_remote_cli_module()

    parser = remote_cli.build_parser()
    install_args = parser.parse_args(["install", "--host", "example.com"])
    modify_args = parser.parse_args(["modify", "--host", "example.com"])
    proof_args = parser.parse_args(["proof", "--host", "example.com"])
    strict_proof_args = parser.parse_args(
        ["proof", "--host", "example.com", "--strict-idempotency"]
    )

    assert install_args.user == "root"
    assert str(install_args.remote_path) == "/root/dokploy-wizard"
    assert str(install_args.env_file) == ".install.env"
    assert install_args.verbose is True

    assert modify_args.user == "root"
    assert str(modify_args.remote_path) == "/root/dokploy-wizard"
    assert str(modify_args.env_file) == ".install.env"
    assert modify_args.verbose is True

    assert proof_args.user == "root"
    assert str(proof_args.remote_path) == "/root/dokploy-wizard"
    assert str(proof_args.env_file) == ".install.env"
    assert proof_args.verbose is True
    assert proof_args.strict_idempotency is False
    assert strict_proof_args.strict_idempotency is True


def test_remote_task1_context_is_explicit_and_forwarded_to_remote_commands() -> None:
    from dokploy_wizard.remote_transport import RemoteTransportSession

    remote_cli = import_remote_cli_module()
    context = Path("/tmp/task1-proof-context.json")
    args = remote_cli.build_parser().parse_args(
        ["proof", "--host", "example.com", "--task1-proof-context", str(context)]
    )
    session = RemoteTransportSession(
        _FakeRemoteTransport(output_callback=None),
        "/root/dokploy-wizard",
        task1_proof_context=context,
    )

    command = remote_cli._build_install_command(session)

    assert args.task1_proof_context == context
    assert "--task1-proof-context" in command
    assert "/root/dokploy-wizard/task1-proof-context.json" in command


@pytest.mark.parametrize(
    "subcommand",
    ["install", "modify", "uninstall", "inspect-state", "proof"],
)
def test_remote_parser_accepts_verbose_for_each_subcommand(subcommand: str) -> None:
    remote_cli = import_remote_cli_module()

    parser = remote_cli.build_parser()
    args = parser.parse_args([subcommand, "--host", "example.com", "--verbose"])

    assert args.verbose is True


@pytest.mark.parametrize(
    "subcommand",
    ["install", "modify", "uninstall", "inspect-state", "proof"],
)
def test_remote_parser_accepts_quiet_remote_output_for_each_subcommand(
    subcommand: str,
) -> None:
    remote_cli = import_remote_cli_module()

    parser = remote_cli.build_parser()
    args = parser.parse_args([subcommand, "--host", "example.com", "--quiet-remote-output"])

    assert args.verbose is False


def test_remote_stream_lines_are_shown_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_cli = import_remote_cli_module()
    env_file = _write_remote_env(tmp_path, packs="nextcloud")
    captured = _patch_successful_remote_run(monkeypatch, remote_cli)

    exit_code = remote_cli.main(
        [
            "install",
            "--host",
            "example.com",
            "--password",
            "super-secret-password",
            "--env-file",
            str(env_file),
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 0
    assert captured["verbose"] is True
    assert "[remote:install:stdout] install streamed" in stderr


def test_quiet_remote_output_suppresses_stream_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_cli = import_remote_cli_module()
    env_file = _write_remote_env(tmp_path, packs="nextcloud")
    captured = _patch_successful_remote_run(monkeypatch, remote_cli)

    exit_code = remote_cli.main(
        [
            "install",
            "--host",
            "example.com",
            "--password",
            "super-secret-password",
            "--env-file",
            str(env_file),
            "--quiet-remote-output",
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 0
    assert captured["verbose"] is False
    assert "[remote:install:stdout]" not in stderr


@pytest.mark.parametrize("subcommand", ["install", "modify", "proof"])
def test_successful_lifecycle_prints_expected_service_urls_for_enabled_service_links(
    subcommand: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_cli = import_remote_cli_module()
    env_file = _write_remote_env(tmp_path, packs="my-farm-advisor,nextcloud,coder")
    _patch_successful_remote_run(monkeypatch, remote_cli)

    exit_code = remote_cli.main(
        [
            subcommand,
            "--host",
            "example.com",
            "--password",
            "super-secret-password",
            "--env-file",
            str(env_file),
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 0
    assert "[remote] expected service URLs:" in stderr
    assert "ready for use" not in stderr
    assert "[remote]   Dokploy: https://dokploy.openmerge.me/" in stderr
    assert "[remote]   Nextcloud: https://nextcloud.openmerge.me/" in stderr
    assert "[remote]   Coder: https://coder.openmerge.me/" in stderr
    assert "[remote]   My Farm Advisor/Farm: https://farm.openmerge.me/" in stderr


@pytest.mark.parametrize(
    ("subcommand", "remote_command"),
    [
        ("install", "install"),
        ("modify", "modify"),
        ("proof", "mutate-install"),
    ],
)
def test_lifecycle_prints_expected_service_urls_before_long_mutation(
    subcommand: str,
    remote_command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_cli = import_remote_cli_module()
    env_file = _write_remote_env(tmp_path, packs="my-farm-advisor,nextcloud")
    _patch_successful_remote_run(monkeypatch, remote_cli)

    exit_code = remote_cli.main(
        [
            subcommand,
            "--host",
            "example.com",
            "--password",
            "super-secret-password",
            "--env-file",
            str(env_file),
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 0
    assert stderr.count("[remote] expected service URLs:") == 1
    assert "ready for use" not in stderr
    url_notice = stderr.index("[remote] expected service URLs:")
    dokploy_link = stderr.index("[remote]   Dokploy: https://dokploy.openmerge.me/")
    started_mutation = stderr.index(f"[remote] starting remote command: {remote_command}")
    assert url_notice < dokploy_link < started_mutation


def test_successful_lifecycle_expected_service_urls_omits_disabled_pack_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_cli = import_remote_cli_module()
    env_file = _write_remote_env(tmp_path, packs="nextcloud")
    _patch_successful_remote_run(monkeypatch, remote_cli)

    exit_code = remote_cli.main(
        [
            "install",
            "--host",
            "example.com",
            "--password",
            "super-secret-password",
            "--env-file",
            str(env_file),
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 0
    assert "[remote] expected service URLs:" in stderr
    assert "ready for use" not in stderr
    assert "[remote]   Dokploy: https://dokploy.openmerge.me/" in stderr
    assert "[remote]   Nextcloud: https://nextcloud.openmerge.me/" in stderr
    assert "Coder:" not in stderr
    assert "My Farm Advisor/Farm:" not in stderr


@pytest.mark.parametrize(
    "subcommand",
    ["install", "modify", "uninstall", "inspect-state", "proof"],
)
def test_each_remote_subcommand_has_help(subcommand: str) -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli(subcommand, "--help")

    assert result.returncode == 0
    assert result.stderr == ""


def test_missing_host_fails_without_echoing_password(tmp_path: Path) -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    password = "super-secret-password"
    env_file = tmp_path / "install.env"
    env_file.write_text(f"VPS_ROOT_PASSWORD={password}\n", encoding="utf-8")

    result = run_cli("install", "--env-file", str(env_file))

    assert result.returncode != 0
    assert "host" in result.stderr.lower()
    assert password not in result.stderr


@pytest.mark.parametrize("subcommand", ["install", "modify", "proof"])
def test_lifecycle_commands_accept_positional_env_file(subcommand: str) -> None:
    remote_cli = import_remote_cli_module()
    parser = remote_cli.build_parser()

    args = parser.parse_args([subcommand, "./.install-my-farm-advisor-min.env"])
    remote_cli._validate_args(parser, args)

    assert str(args.env_file) == ".install-my-farm-advisor-min.env"


def test_runtime_args_derive_host_and_password_from_positional_env_file(tmp_path: Path) -> None:
    remote_cli = import_remote_cli_module()
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "VPS_HOST=env.example.com\nVPS_ROOT_PASSWORD=env-secret-password\n",
        encoding="utf-8",
    )
    parser = remote_cli.build_parser()
    args = parser.parse_args(["modify", str(env_file)])

    remote_cli._validate_args(parser, args)
    remote_cli._validate_runtime_args(parser, args)

    assert args.env_file == env_file
    assert args.host == "env.example.com"
    assert args.password == "env-secret-password"


def test_positional_env_connect_failure_is_clean_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_cli = import_remote_cli_module()
    secret = "secret-test-password"
    env_file = tmp_path / "install.env"
    env_file.write_text(
        f"VPS_HOST=127.0.0.1\nVPS_ROOT_PASSWORD={secret}\n",
        encoding="utf-8",
    )

    def fail_connect(**_kwargs: object) -> object:
        raise RuntimeError(f"authentication failed with password={secret}")

    monkeypatch.setattr(remote_cli.ParamikoRemoteTransport, "connect", fail_connect)

    exit_code = remote_cli.main(["modify", str(env_file), "--verbose"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "[remote] starting remote modify" in captured.err
    assert "[remote] connecting over SSH" in captured.err
    assert "127.0.0.1" not in captured.err
    assert "Traceback" not in captured.err
    assert secret not in captured.err
    assert "<REDACTED>" in captured.err


def test_explicit_host_and_password_override_env_file_values(tmp_path: Path) -> None:
    remote_cli = import_remote_cli_module()
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "VPS_HOST=env.example.com\nVPS_ROOT_PASSWORD=env-secret-password\n",
        encoding="utf-8",
    )
    parser = remote_cli.build_parser()
    args = parser.parse_args(
        [
            "proof",
            str(env_file),
            "--host",
            "flag.example.com",
            "--password",
            "flag-secret-password",
        ]
    )

    remote_cli._validate_args(parser, args)
    remote_cli._validate_runtime_args(parser, args)

    assert args.host == "flag.example.com"
    assert args.password == "flag-secret-password"


def test_positional_env_file_conflicts_with_different_env_file_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote_cli = import_remote_cli_module()
    positional_env = tmp_path / "positional.env"
    flag_env = tmp_path / "flag.env"
    parser = remote_cli.build_parser()
    args = parser.parse_args(["install", str(positional_env), "--env-file", str(flag_env)])

    with pytest.raises(SystemExit):
        remote_cli._validate_args(parser, args)

    captured = capsys.readouterr()
    assert "positional env file and --env-file refer to different paths" in captured.err


def test_install_help_surfaces_fresh_flag() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli("install", "--help")

    assert result.returncode == 0
    assert "--fresh" in result.stdout
    assert result.stderr == ""


def test_proof_help_surfaces_strict_idempotency_flag() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli("proof", "--help")

    assert result.returncode == 0
    assert "--strict-idempotency" in result.stdout
    assert result.stderr == ""


def test_uninstall_rejects_fresh_flag() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli("uninstall", "--host", "example.com", "--fresh")

    assert result.returncode != 0
    assert "fresh" in result.stderr.lower()


def test_fresh_requires_confirm_file() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli("install", "--host", "example.com", "--fresh")

    assert result.returncode != 0
    assert "confirm-file" in result.stderr.lower()
    assert "fresh" in result.stderr.lower()
    assert "connection" not in result.stderr.lower()


def test_fresh_is_not_applicable_to_uninstall() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli(
        "uninstall",
        "--host",
        "example.com",
        "--fresh",
        "--destroy-data",
    )

    assert result.returncode != 0
    assert "fresh" in result.stderr.lower()
    assert "uninstall" in result.stderr.lower()


def test_fresh_validation_errors_redact_password() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    password = "super-secret-password"
    result = run_cli(
        "install",
        "--host",
        "example.com",
        "--password",
        password,
        "--fresh",
    )

    assert result.returncode != 0
    assert "confirm-file" in result.stderr.lower()
    assert password not in result.stderr


def test_missing_env_file_verbose_fails_cleanly_without_traceback_or_password() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    password = "super-secret-password"
    result = run_cli(
        "proof",
        "--host",
        "example.com",
        "--password",
        password,
        "--env-file",
        "/tmp/does-not-exist",
        "--verbose",
    )

    assert result.returncode != 0
    assert "install env file does not exist: /tmp/does-not-exist" in result.stderr
    assert "[remote] starting remote proof" in result.stderr
    assert "[remote] remote proof failed" in result.stderr
    assert "Traceback" not in result.stderr
    assert password not in result.stderr


def _create_committed_archive_fixture(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src" / "dokploy_wizard").mkdir(parents=True)
    (repo_root / "bin").mkdir()
    (repo_root / "templates").mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "src" / "dokploy_wizard" / "__init__.py").write_text("\n", encoding="utf-8")
    (repo_root / "bin" / "dokploy-wizard").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo_root / "templates" / "deploy.yaml").write_text("services: {}\n", encoding="utf-8")
    (repo_root / "scripts" / "release_activation_bootstrap.py").write_text(
        "#!/usr/bin/env python3\n",
        encoding="utf-8",
    )
    (repo_root / ".install.env.example").write_text("ROOT_DOMAIN=example.com\n", encoding="utf-8")
    (repo_root / ".gitignore").write_text(
        ".*.env\n.playwright-mcp/\n.omo/\n.codegraph/\n",
        encoding="utf-8",
    )

    init = subprocess.run(
        ["git", "-C", str(repo_root), "init"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stderr
    add = subprocess.run(
        ["git", "-C", str(repo_root), "add", "."],
        check=False,
        capture_output=True,
        text=True,
    )
    assert add.returncode == 0, add.stderr
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "user.name=Archive Test",
            "-c",
            "user.email=archive-test@example.invalid",
            "commit",
            "-m",
            "archive fixture",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert commit.returncode == 0, commit.stderr
    return repo_root


def test_task1_cleanup_cli_writes_only_exact_remote_json_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    remote_cli = import_remote_cli_module()
    cleanup_json = b'{"status":"complete","schema_version":1}\n'
    transport = _Task1CleanupTransport(cleanup_json)
    env_file = _write_remote_env(tmp_path, packs="nextcloud")

    monkeypatch.setattr(
        remote_cli.ParamikoRemoteTransport,
        "connect",
        lambda **_kwargs: transport,
    )
    monkeypatch.setattr(remote_cli, "_validate_task1_proof_context", lambda _args: None)

    exit_code = remote_cli.main(
        [
            "task1-cleanup",
            "--host",
            "cleanup.example.test",
            "--password",
            "cleanup-password-sentinel",
            "--env-file",
            str(env_file),
            "--task1-proof-context",
            str(tmp_path / "task1-proof-context.json"),
        ]
    )

    captured = capsysbinary.readouterr()

    assert exit_code == 0
    assert captured.out == cleanup_json
    assert json.loads(captured.out) == {"status": "complete", "schema_version": 1}
    assert b"[remote" not in captured.out
    assert b"cleanup.example.test" not in captured.out
    assert b"cleanup-password-sentinel" not in captured.out
    assert b"[remote] starting remote task1-cleanup" in captured.err
    assert len(transport.capture_calls) == 1
    assert transport.capture_calls[0][0] == "task1-cloudflare-cleanup"


def test_task1_cleanup_classifies_absent_journal_without_remote_stderr(
    tmp_path: Path,
) -> None:
    remote_cli = import_remote_cli_module()
    from dokploy_wizard.remote_transport import (
        RemoteCapturedCommandFailure,
        RemoteTransportSession,
    )

    class JournalAbsentTransport(_Task1CleanupTransport):
        def capture(self, subcommand: str, command: str, _limits: Any) -> Any:
            self.capture_calls.append((subcommand, command))
            raise RemoteCapturedCommandFailure(
                subcommand=subcommand,
                reason="nonzero status",
                stderr=(
                    b"Task 1 Cloudflare cleanup failed: "
                    b"Task 1 Cloudflare cleanup journal is absent\n"
                ),
                exit_status=1,
            )

    session = RemoteTransportSession(
        JournalAbsentTransport(b""),
        "/root/dokploy-wizard",
        task1_proof_context=tmp_path / "task1-proof-context.json",
    )

    with pytest.raises(RuntimeError, match="Task 1 Cloudflare cleanup journal is absent") as error:
        remote_cli._run_task1_cleanup(session=session, password="password-sentinel")

    assert "password-sentinel" not in str(error.value)


def test_create_repo_archive_includes_only_committed_tree_members(tmp_path: Path) -> None:
    remote_cli = import_remote_cli_module()
    repo_root = _create_committed_archive_fixture(tmp_path)
    untracked_paths = (
        ".install.env",
        ".install-min.env",
        ".install.env.backup",
        ".playwright-mcp/session.json",
        ".omo/plan.json",
        ".codegraph/index.sqlite",
        "untracked-sentinel.txt",
    )
    for relative_path in untracked_paths:
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("untracked sentinel\n", encoding="utf-8")

    archive_path = tmp_path / "repo.tar.gz"
    remote_cli._create_repo_archive(repo_root=repo_root, destination=archive_path)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getnames()

    for committed_path in (
        "src/dokploy_wizard/__init__.py",
        "bin/dokploy-wizard",
        "templates/deploy.yaml",
        ".install.env.example",
    ):
        assert members.count(committed_path) == 1
    for untracked_path in untracked_paths:
        assert untracked_path not in members


def test_create_repo_archive_requires_a_committed_head(tmp_path: Path) -> None:
    remote_cli = import_remote_cli_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    init = subprocess.run(
        ["git", "-C", str(repo_root), "init"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stderr

    archive_path = tmp_path / "repo.tar.gz"

    with pytest.raises(RuntimeError):
        remote_cli._create_repo_archive(repo_root=repo_root, destination=archive_path)


def test_create_repo_archive_fails_closed_when_git_archive_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_cli = import_remote_cli_module()
    repo_root = _create_committed_archive_fixture(tmp_path)

    def fail_create(**_kwargs: object) -> None:
        raise remote_cli.ReleaseError("git archive command failed")

    monkeypatch.setattr(
        remote_cli,
        "create_commit_archive",
        fail_create,
    )

    with pytest.raises(remote_cli.RepositoryArchiveError, match="git archive command failed"):
        remote_cli._create_repo_archive(repo_root=repo_root, destination=tmp_path / "repo.tar.gz")


def test_remote_runtime_hydrates_connection_from_selected_env_file_unchanged(
    tmp_path: Path,
) -> None:
    remote_cli = import_remote_cli_module()
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "VPS_HOST=baseline.example.com\nVPS_ROOT_PASSWORD=baseline-password\n",
        encoding="utf-8",
    )
    parser = remote_cli.build_parser()
    args = parser.parse_args(["inspect-state", "--env-file", str(env_file)])

    remote_cli._validate_args(parser, args)
    remote_cli._validate_runtime_args(parser, args)

    assert args.host == "baseline.example.com"
    assert args.password == "baseline-password"


def test_runtime_error_redaction_masks_env_payload_values() -> None:
    remote_cli = import_remote_cli_module()
    password = "super-secret-password"
    sentinel = "SECRET_TEST_OPENCLAW_PROVIDER_VALUE"

    message = remote_cli._redact_runtime_message(
        (
            f"ssh failed with {password}\n"
            "# dokploy-wizard-env marker=dokploy-wizard owner=openclaw "
            "key=OPENCLAW_PROVIDER_API_KEY fingerprint=sha256:def456\n"
            f"OPENCLAW_PROVIDER_API_KEY={sentinel}"
        ),
        password=password,
    )

    assert password not in message
    assert sentinel not in message
    assert "OPENCLAW_PROVIDER_API_KEY=<REDACTED>" in message
    assert "fingerprint=sha256:def456" in message


def test_verbose_progress_output_redacts_password_env_payload_and_docker_pat() -> None:
    remote_cli = import_remote_cli_module()
    password = "super-secret-password"
    env_secret = "SECRET_TEST_OPENCLAW_PROVIDER_VALUE"
    docker_pat = "ghp_SECRETTESTDOCKERPATVALUE"
    stream = io.StringIO()
    reporter = remote_cli._RemoteProgressReporter(
        verbose=True,
        password=password,
        stream=stream,
    )

    reporter.progress(f"connecting with password={password}")
    reporter.remote_output(
        "mutate-install",
        "stdout",
        "# dokploy-wizard-env marker=dokploy-wizard owner=openclaw "
        "key=OPENCLAW_PROVIDER_API_KEY fingerprint=sha256:def456",
    )
    reporter.remote_output(
        "mutate-install",
        "stdout",
        f"OPENCLAW_PROVIDER_API_KEY={env_secret}",
    )
    reporter.remote_output("mutate-install", "stderr", f"DOCKER_PAT={docker_pat}")

    output = stream.getvalue()
    assert password not in output
    assert env_secret not in output
    assert docker_pat not in output
    assert "OPENCLAW_PROVIDER_API_KEY=<REDACTED>" in output
    assert "DOCKER_PAT=<REDACTED>" in output


def test_non_verbose_progress_output_suppresses_remote_stream_lines() -> None:
    remote_cli = import_remote_cli_module()
    stream = io.StringIO()
    reporter = remote_cli._RemoteProgressReporter(
        verbose=False,
        password="super-secret-password",
        stream=stream,
    )

    reporter.remote_output("mutate-install", "progress", "still running mutate-install")
    reporter.remote_output("mutate-install", "stdout", "normal install output")

    assert stream.getvalue() == ""
