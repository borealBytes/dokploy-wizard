from __future__ import annotations

import importlib
import subprocess
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

    assert install_args.user == "root"
    assert str(install_args.remote_path) == "/root/dokploy-wizard"
    assert str(install_args.env_file) == ".install.env"

    assert modify_args.user == "root"
    assert str(modify_args.remote_path) == "/root/dokploy-wizard"
    assert str(modify_args.env_file) == ".install.env"

    assert proof_args.user == "root"
    assert str(proof_args.remote_path) == "/root/dokploy-wizard"
    assert str(proof_args.env_file) == ".install.env"


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
