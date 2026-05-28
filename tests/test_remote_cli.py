from __future__ import annotations

import importlib
import io
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

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

    assert modify_args.user == "root"
    assert str(modify_args.remote_path) == "/root/dokploy-wizard"
    assert str(modify_args.env_file) == ".install.env"

    assert proof_args.user == "root"
    assert str(proof_args.remote_path) == "/root/dokploy-wizard"
    assert str(proof_args.env_file) == ".install.env"
    assert proof_args.strict_idempotency is False
    assert strict_proof_args.strict_idempotency is True


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
    assert "connecting to root@127.0.0.1:22" in captured.err
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


def test_create_repo_archive_excludes_local_env_backups(tmp_path: Path) -> None:
    remote_cli = import_remote_cli_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "keep.txt").write_text("safe\n", encoding="utf-8")
    (repo_root / ".install.env.example").write_text("safe-example\n", encoding="utf-8")
    (repo_root / ".install.env.bak").write_text("secret\n", encoding="utf-8")
    (repo_root / ".install.env.swp").write_text("secret\n", encoding="utf-8")
    (repo_root / ".fresh-vps-validation.env.backup").write_text("secret\n", encoding="utf-8")

    archive_path = tmp_path / "repo.tar.gz"
    remote_cli._create_repo_archive(repo_root=repo_root, destination=archive_path)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = set(archive.getnames())

    assert "keep.txt" in members
    assert ".install.env.example" in members
    assert ".install.env.bak" not in members
    assert ".install.env.swp" not in members
    assert ".fresh-vps-validation.env.backup" not in members


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
