from __future__ import annotations

import json
from pathlib import Path

from dokploy_wizard.service_verification_runner import _merge_persisted_retry_keys, main
from dokploy_wizard.state import RawEnvInput


def test_main_returns_success_and_prints_payload(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    env_file = tmp_path / ".install.env"
    env_file.write_text("ROOT_DOMAIN=example.com\n", encoding="utf-8")

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.run_service_verification",
        lambda **_: {"passed": True, "results": [{"service_name": "shared-core", "status": "pass"}]},
    )

    exit_code = main(["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "passed": True,
        "results": [{"service_name": "shared-core", "status": "pass"}],
    }


def test_main_returns_failure_for_failed_payload(monkeypatch, tmp_path: Path, capsys) -> None:
    env_file = tmp_path / ".install.env"
    env_file.write_text("ROOT_DOMAIN=example.com\n", encoding="utf-8")

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.run_service_verification",
        lambda **_: {"passed": False, "results": [{"service_name": "coder", "status": "fail"}]},
    )

    exit_code = main(["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_merge_persisted_retry_keys_prefers_persisted_auth_values() -> None:
    raw_env = RawEnvInput(format_version=1, values={"ROOT_DOMAIN": "example.com"})
    persisted_raw = RawEnvInput(
        format_version=1,
        values={
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_API_KEY": "persisted-key",
            "DOKPLOY_API_URL": "https://dokploy.example.com/api",
        },
    )

    merged = _merge_persisted_retry_keys(raw_env, persisted_raw)

    assert merged.values["DOKPLOY_API_KEY"] == "persisted-key"
    assert merged.values["DOKPLOY_API_URL"] == "https://dokploy.example.com/api"
