from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dokploy_wizard.litellm import LiteLLMAdminUiCheckError
from dokploy_wizard.service_verification_runner import (
    _merge_persisted_retry_keys,
    _verify_backend_method,
    _verify_litellm_admin_ui,
    main,
    run_service_verification,
)
from dokploy_wizard.state import RawEnvInput
from dokploy_wizard.verification import ServiceVerificationResult, make_verification_result


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
    verified_services: list[str] = []

    class FakeLoadedState:
        raw_input = None

    def fake_result(**_kwargs: Any) -> Any:
        return type("Result", (), {"passed": True, "to_dict": lambda self: {}})()

    def record_verified_backend(**kwargs: Any) -> Any:
        verified_services.append(str(kwargs["service_name"]))
        return fake_result()

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
        "dokploy_wizard.service_verification_runner._verify_shared_core",
        fake_result,
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner._verify_litellm_admin_ui",
        fake_result,
    )
    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner._verify_backend_method",
        record_verified_backend,
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
    # Disabled packs (seaweedfs, coder) build backends but are never verified;
    # verification is gated on the enabled pack set.
    assert verified_services == ["nextcloud", "moodle", "docuseal"]


class _FakePackBackend:
    def __init__(self, result: object) -> None:
        self._result = result

    def _verify_current_application(self) -> object:
        return self._result


def test_verify_backend_method_returns_the_backend_result_verbatim() -> None:
    expected: ServiceVerificationResult = make_verification_result(
        service_name="nextcloud",
        tier="app",
        passed=True,
        detail="Nextcloud migrations, cron, and OnlyOffice readiness checks passed.",
    )

    result = _verify_backend_method(
        backend=_FakePackBackend(expected),
        method_name="_verify_current_application",
        service_name="nextcloud",
        unavailable_detail="Nextcloud verification backend is unavailable.",
    )

    assert result is expected
    assert result.passed is True
    assert "secret-123" not in result.detail


def test_verify_backend_method_fails_when_pack_backend_is_unavailable() -> None:
    result = _verify_backend_method(
        backend=object(),
        method_name="_verify_current_application",
        service_name="moodle",
        unavailable_detail="Moodle verification backend is unavailable.",
    )

    assert result.status == "fail"
    assert result.passed is False
    assert result.detail == "Moodle verification backend is unavailable."


def test_verify_backend_method_rejects_non_verification_result() -> None:
    with pytest.raises(TypeError, match="Expected ServiceVerificationResult from coder"):
        _verify_backend_method(
            backend=_FakePackBackend(object()),
            method_name="_verify_current_application",
            service_name="coder",
            unavailable_detail="Coder verification backend is unavailable.",
        )


def test_verify_litellm_admin_ui_reports_the_internal_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: dict[str, str] = {}

    def _serves(url: str) -> None:
        requested["url"] = url

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.verify_internal_litellm_admin_ui", _serves
    )
    passed = _verify_litellm_admin_ui()

    assert passed.passed is True
    assert "answered 200" in passed.detail
    # The request must be the loopback-published port: the public hostname answers 302
    # for a broken UI as readily as for a working one.
    assert requested["url"] == "http://127.0.0.1:4000/ui"

    def _broken(url: str) -> None:
        raise LiteLLMAdminUiCheckError("LiteLLM admin UI returned status 404; expected 200.")

    monkeypatch.setattr(
        "dokploy_wizard.service_verification_runner.verify_internal_litellm_admin_ui", _broken
    )
    failed = _verify_litellm_admin_ui()

    assert failed.passed is False
    assert "404" in failed.detail
    assert "/ui" in failed.detail
