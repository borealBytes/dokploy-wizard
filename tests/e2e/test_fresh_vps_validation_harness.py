from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dokploy_wizard import fresh_vps_validation_harness as harness

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "bin" / "fresh-vps-validation-harness"


def _run_harness(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HARNESS), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _write_install_env(path: Path) -> Path:
    path.write_text(
        "ROOT_DOMAIN=example.com\n"
        "STACK_NAME=example\n"
        "PACKS=nextcloud\n"
        "DOKPLOY_API_URL=http://127.0.0.1:3000\n",
        encoding="utf-8",
    )
    return path


def test_harness_dry_run_renders_proof_plan_and_bundle(tmp_path: Path) -> None:
    install_env = _write_install_env(tmp_path / ".install.env")
    artifact_root = tmp_path / "artifacts"

    result = _run_harness(
        "--dry-run",
        "--install-env-file",
        str(install_env),
        "--artifact-root",
        str(artifact_root),
        "--label",
        "dry-run-proof",
        "--target-host",
        "203.0.113.10",
        "--target-user",
        "root",
        "--target-path",
        "/srv/dokploy-proof",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry_run"
    assert payload["missing_required_settings"] == []
    assert payload["steps"] == [
        "package_repo",
        "upload_bundle",
        "extract_repo",
        "place_install_env",
        "first_install",
        "rerun_same_host_noop_proof",
        "inspect_state",
        "collect_state_and_logs",
    ]
    assert Path(payload["archive_path"]).exists()
    assert Path(payload["commands_path"]).exists()
    assert Path(payload["remote_script_path"]).exists()
    assert payload["remote_plan"]["remote_install_env_path"].endswith("/.install.env")
    assert payload["config"]["target_host"] == "203.0.113.10"
    assert payload["config"]["target_user"] == "root"
    assert payload["config"]["target_path"] == "/srv/dokploy-proof"


def test_harness_self_check_simulates_extract_and_env_placement(tmp_path: Path) -> None:
    install_env = _write_install_env(tmp_path / ".install.env")
    artifact_root = tmp_path / "artifacts"

    result = _run_harness(
        "--self-check",
        "--install-env-file",
        str(install_env),
        "--artifact-root",
        str(artifact_root),
        "--label",
        "self-check-proof",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "self_check"
    self_check = payload["self_check"]
    assert self_check["install_env_matches_source"] is True
    assert self_check["install_env_mode"] == "0o600"
    assert self_check["tarball_contains_bin_wrapper"] is True
    assert self_check["tarball_contains_source_module"] is True
    assert self_check["script_mentions_noop_proof"] is True
    assert Path(self_check["install_env_placed"]).exists()
    assert Path(self_check["extracted_repo"]).joinpath("bin", "dokploy-wizard").exists()


def test_harness_reads_ignored_style_config_file(tmp_path: Path) -> None:
    install_env = _write_install_env(tmp_path / ".install.env")
    artifact_root = tmp_path / "artifacts"
    config_file = tmp_path / ".fresh-vps-validation.env"
    config_file.write_text(
        "HOST=198.51.100.24\n"
        "USER=ubuntu\n"
        "PATH=/opt/dokploy-proof\n"
        f"INSTALL_ENV_FILE={install_env}\n"
        f"ARTIFACT_ROOT={artifact_root}\n"
        "LABEL=config-proof\n",
        encoding="utf-8",
    )

    result = _run_harness(
        "--dry-run",
        "--config-file",
        str(config_file),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry_run"
    assert payload["config"]["config_file"] == str(config_file)
    assert payload["config"]["install_env_file"] == str(install_env)
    assert payload["config"]["target_host"] == "198.51.100.24"
    assert payload["config"]["target_user"] == "ubuntu"
    assert payload["config"]["target_path"] == "/opt/dokploy-proof"
    assert payload["run_label"] == "config-proof"


def test_execute_mode_collects_remote_artifacts_after_remote_proof_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_env = _write_install_env(tmp_path / ".install.env")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    archive_path = artifact_dir / "repo.tar.gz"
    archive_path.write_text("placeholder", encoding="utf-8")
    config = harness.HarnessConfig(
        repo_root=REPO_ROOT,
        config_file=None,
        install_env_file=install_env,
        artifact_root=artifact_dir,
        target_host="203.0.113.10",
        target_user="root",
        target_path="/srv/dokploy-proof",
        ssh_port=22,
        ssh_options=(),
        label="proof-failure",
    )
    plan = harness.build_remote_plan(config=config, run_label="proof-failure")
    plan_payload = {"mode": "execute", "steps": ["run-remote-proof", "collect-state-and-logs"]}
    calls: list[tuple[str, bool]] = []

    def fake_run_logged_command(
        command: list[str],
        *,
        label: str,
        commands: list[dict[str, object]],
        artifact_dir: Path,
        raise_on_error: bool = True,
    ) -> dict[str, object]:
        del command, artifact_dir
        calls.append((label, raise_on_error))
        exit_code = 1 if label == "run-remote-proof" else 0
        record: dict[str, object] = {
            "command": [label],
            "exit_code": exit_code,
            "label": label,
            "stderr_path": str(tmp_path / f"{label}.stderr"),
            "stdout_path": str(tmp_path / f"{label}.stdout"),
        }
        commands.append(record)
        if raise_on_error and exit_code != 0:
            raise RuntimeError(f"{label} failed with exit code {exit_code}")
        return record

    monkeypatch.setattr(harness, "run_logged_command", fake_run_logged_command)

    with pytest.raises(RuntimeError, match="run-remote-proof failed with exit code 1"):
        harness.run_execute_mode(
            config=config,
            plan=plan,
            archive_path=archive_path,
            artifact_dir=artifact_dir,
            plan_payload=plan_payload,
        )

    assert calls == [
        ("prepare-remote-root", True),
        ("upload-archive", True),
        ("upload-install-env", True),
        ("upload-remote-script", True),
        ("run-remote-proof", False),
        ("collect-remote-artifacts", True),
    ]
