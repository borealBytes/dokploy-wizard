from __future__ import annotations

import importlib
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
def test_each_remote_subcommand_has_help(subcommand: str) -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    result = run_cli(subcommand, "--help")

    assert result.returncode == 0
    assert result.stderr == ""


def test_missing_host_fails_without_echoing_password() -> None:
    assert CLI.exists(), f"expected remote CLI wrapper at {CLI}"

    password = "super-secret-password"
    result = run_cli("install", "--password", password)

    assert result.returncode != 0
    assert "host" in result.stderr.lower()
    assert password not in result.stderr


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
