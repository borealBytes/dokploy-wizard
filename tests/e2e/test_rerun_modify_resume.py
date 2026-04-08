from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "bin" / "dokploy-wizard"
FIXTURES_DIR = REPO_ROOT / "fixtures"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _write_env(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _replace_line(content: str, key: str, value: str) -> str:
    prefix = f"{key}="
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return content + f"\n{key}={value}\n"


def _seed_mock_existing_state(
    state_dir: Path,
    *,
    root_domain: str,
    headscale_healthy: str | None = None,
    include_headscale_service: bool = True,
) -> None:
    raw_input_path = state_dir / "raw-input.json"
    payload = json.loads(raw_input_path.read_text(encoding="utf-8"))
    values = payload["values"]
    values["CLOUDFLARE_MOCK_EXISTING_HOSTNAMES"] = f"dokploy.{root_domain},headscale.{root_domain}"
    if include_headscale_service:
        values["HEADSCALE_MOCK_EXISTING_SERVICE_ID"] = "lifecycle-stack-headscale"
    else:
        values.pop("HEADSCALE_MOCK_EXISTING_SERVICE_ID", None)
    if headscale_healthy is not None:
        values["HEADSCALE_MOCK_HEALTHY"] = headscale_healthy
    raw_input_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_cli_install_then_rerun_surfaces_explicit_noop(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    base_env = (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8")
    install_env = _write_env(tmp_path / "install.env", base_env)
    rerun_env = _write_env(tmp_path / "rerun.env", base_env)

    first = _run_cli(
        "install",
        "--env-file",
        str(install_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )
    _seed_mock_existing_state(state_dir, root_domain="example.com", headscale_healthy="true")
    rerun_env = _write_env(
        tmp_path / "rerun.env",
        base_env
        + "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,headscale.example.com"
        + "\nHEADSCALE_MOCK_EXISTING_SERVICE_ID=lifecycle-stack-headscale\n",
    )
    second = _run_cli(
        "install",
        "--env-file",
        str(rerun_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["lifecycle"]["mode"] == "noop"
    assert second_payload["state_status"] == "existing"


def test_cli_modify_domain_reconciles_supported_change(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    install_env = _write_env(
        tmp_path / "install.env",
        (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
    )

    installed = _run_cli(
        "install",
        "--env-file",
        str(install_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )
    modify_env = _write_env(
        tmp_path / "modify.env",
        _replace_line(
            (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8"),
            "ROOT_DOMAIN",
            "example.net",
        ),
    )
    modified = _run_cli(
        "modify",
        "--env-file",
        str(modify_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert installed.returncode == 0, installed.stderr
    assert modified.returncode == 0, modified.stderr
    modified_payload = json.loads(modified.stdout)
    assert modified_payload["lifecycle"]["mode"] == "modify"
    assert modified_payload["lifecycle"]["start_phase"] == "networking"
    assert modified_payload["headscale"]["outcome"] in {"already_present", "applied"}


def test_cli_resume_continues_from_failed_headscale_phase(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    base_env = (FIXTURES_DIR / "lifecycle-headscale.env").read_text(encoding="utf-8")
    failing_env = _write_env(
        tmp_path / "failing.env",
        _replace_line(base_env, "HEADSCALE_MOCK_HEALTHY", "false"),
    )
    resume_env = _write_env(
        tmp_path / "resume.env",
        _replace_line(base_env, "HEADSCALE_MOCK_HEALTHY", "true")
        + "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,headscale.example.com\n",
    )

    failed = _run_cli(
        "install",
        "--env-file",
        str(failing_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )
    _seed_mock_existing_state(
        state_dir,
        root_domain="example.com",
        headscale_healthy="true",
        include_headscale_service=False,
    )
    resumed = _run_cli(
        "install",
        "--env-file",
        str(resume_env),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert failed.returncode != 0
    assert "headscale health check failed" in failed.stderr.lower()
    assert resumed.returncode == 0, resumed.stderr
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["lifecycle"]["mode"] == "resume"
    assert resumed_payload["lifecycle"]["start_phase"] == "headscale"


def test_cli_modify_rejects_unsupported_stack_name_change(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    installed = _run_cli(
        "install",
        "--env-file",
        str(FIXTURES_DIR / "lifecycle-headscale.env"),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )
    _seed_mock_existing_state(state_dir, root_domain="example.com")
    modified = _run_cli(
        "modify",
        "--env-file",
        str(FIXTURES_DIR / "modify-unsupported.env"),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    )

    assert installed.returncode == 0, installed.stderr
    assert modified.returncode != 0
    assert "STACK_NAME changes are unsupported" in modified.stderr
