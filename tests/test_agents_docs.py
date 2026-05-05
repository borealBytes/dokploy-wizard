# mypy: ignore-errors
# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
BIN_AGENTS_MD = REPO_ROOT / "bin" / "AGENTS.md"


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_lower(path: Path) -> str:
    return _read(path).lower()


def test_root_agents_md_exists() -> None:
    assert AGENTS_MD.exists(), (
        f"Root AGENTS.md must exist at {AGENTS_MD} — "
        "it is the canonical deployment guidance for agents"
    )


def test_bin_agents_md_exists() -> None:
    assert BIN_AGENTS_MD.exists(), (
        f"bin/AGENTS.md must exist at {BIN_AGENTS_MD} — "
        "it documents the bin shim pattern and wrapper conventions"
    )


def test_root_agents_md_has_project_overview() -> None:
    text = _read_lower(AGENTS_MD)
    assert "project overview" in text or "overview" in text, (
        "Root AGENTS.md must contain a project overview section "
        "so agents understand what Dokploy Wizard deploys"
    )


def test_root_agents_md_has_local_commands() -> None:
    text = _read_lower(AGENTS_MD)
    assert "local commands" in text or "local" in text, (
        "Root AGENTS.md must document local commands "
        "(install, modify, uninstall, inspect-state)"
    )


def test_root_agents_md_has_remote_deployment() -> None:
    text = _read_lower(AGENTS_MD)
    assert "remote deployment" in text or "remote" in text, (
        "Root AGENTS.md must document how to deploy to a fresh remote VPS"
    )


def test_root_agents_md_has_install_env_guidance() -> None:
    text = _read_lower(AGENTS_MD)
    assert ".install.env" in text or "install.env" in text, (
        "Root AGENTS.md must reference .install.env as the operator env file"
    )


def test_root_agents_md_has_secret_handling() -> None:
    text = _read_lower(AGENTS_MD)
    assert "secret" in text, (
        "Root AGENTS.md must contain secret handling guidance "
        "(redaction, 0600 permissions, no logging)"
    )


def test_root_agents_md_has_testing_section() -> None:
    text = _read_lower(AGENTS_MD)
    assert "testing" in text or "test" in text, (
        "Root AGENTS.md must contain a testing section with pytest / ruff / mypy commands"
    )


def test_root_agents_md_has_safety_section() -> None:
    text = _read_lower(AGENTS_MD)
    assert "safety" in text, (
        "Root AGENTS.md must contain a safety section covering destructive operations"
    )


def test_root_agents_md_references_bin_agents_md() -> None:
    text = _read_lower(AGENTS_MD)
    assert "bin/agents.md" in text or "bin/agents" in text, (
        "Root AGENTS.md must reference bin/AGENTS.md for shim-specific guidance"
    )


def test_bin_agents_md_has_shim_pattern() -> None:
    text = _read_lower(BIN_AGENTS_MD)
    assert "shim" in text, (
        "bin/AGENTS.md must document the bin shim pattern "
        "(how bin/dokploy-wizard wraps the Python entrypoint)"
    )


def test_bin_agents_md_has_wrapper_naming() -> None:
    text = _read_lower(BIN_AGENTS_MD)
    assert "wrapper" in text or "naming" in text, (
        "bin/AGENTS.md must document wrapper naming conventions"
    )


def test_bin_agents_md_has_remote_helper_usage() -> None:
    text = _read_lower(BIN_AGENTS_MD)
    assert "remote" in text or "helper" in text or "fresh-vps" in text, (
        "bin/AGENTS.md must document remote helper usage for fresh-VPS deploys"
    )


def test_bin_agents_md_has_no_secret_logging() -> None:
    text = _read_lower(BIN_AGENTS_MD)
    assert "no-secret" in text or "no secret" in text or "redact" in text, (
        "bin/AGENTS.md must contain no-secret logging guidance"
    )


def test_agents_md_does_not_claim_key_based_ssh() -> None:
    text = _read_lower(AGENTS_MD)
    bad = "key-based ssh" in text or "ssh key" in text or "passwordless ssh" in text
    assert not bad, (
        "AGENTS.md must not claim key-based SSH is supported; "
        "the fresh-VPS harness uses password auth"
    )


def test_agents_md_does_not_claim_multi_host() -> None:
    text = _read_lower(AGENTS_MD)
    bad = "multi-host" in text or "multi host" in text or "multiple hosts" in text
    assert not bad, (
        "AGENTS.md must not claim multi-host deployment; "
        "the wizard targets a single fresh VPS"
    )


def test_bin_agents_md_does_not_claim_key_based_ssh() -> None:
    text = _read_lower(BIN_AGENTS_MD)
    bad = "key-based ssh" in text or "ssh key" in text or "passwordless ssh" in text
    assert not bad, (
        "bin/AGENTS.md must not claim key-based SSH is supported"
    )


def test_bin_agents_md_does_not_claim_multi_host() -> None:
    text = _read_lower(BIN_AGENTS_MD)
    bad = "multi-host" in text or "multi host" in text or "multiple hosts" in text
    assert not bad, (
        "bin/AGENTS.md must not claim multi-host deployment"
    )


def test_agents_md_secret_guidance_is_present() -> None:
    text = _read_lower(AGENTS_MD)
    has_guidance = (
        "redact" in text
        and "0600" in text
        and "password" in text
        and "do not log" in text
    )
    assert has_guidance, (
        "AGENTS.md must contain explicit secret/password guidance: "
        "redaction, 0600 permissions, and 'do not log' instructions"
    )
