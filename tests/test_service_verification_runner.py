from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard.service_verification_runner import (
    _merge_persisted_retry_keys,
    main,
    run_service_verification,
)
from dokploy_wizard.state import RawEnvInput


def test_main_returns_success_and_prints_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / ".install.env"
    env_file.write_text("ROOT_DOMAIN=example.com\n", encoding="utf-8")

    success_payload = {
        "passed": True,
        "results": [{"service_name": "shared-core", "status": "pass"}],
    }
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.run_service_verification",
        lambda **_: success_payload,
    )

    exit_code = main(["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "passed": True,
        "results": [{"service_name": "shared-core", "status": "pass"}],
    }


def test_main_returns_failure_for_failed_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / ".install.env"
    env_file.write_text("ROOT_DOMAIN=example.com\n", encoding="utf-8")

    failed_payload = {
        "passed": False,
        "results": [{"service_name": "coder", "status": "fail"}],
    }
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.run_service_verification",
        lambda **_: failed_payload,
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


def test_run_service_verification_passes_state_dir_to_compose_builders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".install.env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_NAME=wizard-stack",
                "ROOT_DOMAIN=example.com",
                "DOKPLOY_API_URL=https://dokploy.example.com/api",
                "DOKPLOY_API_KEY=dokploy-key",
                "DOKPLOY_ADMIN_EMAIL=admin@example.com",
                "DOKPLOY_ADMIN_PASSWORD=secret-123",
                "PACKS=nextcloud,moodle,docuseal",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    seen: dict[str, Path] = {}

    class FakeLoadedState:
        raw_input = None

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.load_state_dir",
        lambda state_dir: FakeLoadedState(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.load_litellm_generated_keys",
        lambda state_dir: None,
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_dokploy_session_client",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_shared_core_backend",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_seaweedfs_backend",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_coder_backend",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_openclaw_backend",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner._verify_shared_core",
        lambda **kwargs: type("Result", (), {"passed": True, "to_dict": lambda self: {}})(),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner._verify_backend_method",
        lambda **kwargs: type("Result", (), {"passed": True, "to_dict": lambda self: {}})(),
    )

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_nextcloud_backend",
        lambda **kwargs: seen.setdefault("nextcloud", kwargs["state_dir"]),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_moodle_backend",
        lambda **kwargs: seen.setdefault("moodle", kwargs["state_dir"]),
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.cli._build_docuseal_backend",
        lambda **kwargs: seen.setdefault("docuseal", kwargs["state_dir"]),
    )

    payload = run_service_verification(env_file=env_file, state_dir=state_dir)

    assert payload["passed"] is True
    assert seen == {
        "nextcloud": state_dir,
        "moodle": state_dir,
        "docuseal": state_dir,
    }
