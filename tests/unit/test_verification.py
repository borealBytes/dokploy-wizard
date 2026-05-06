from __future__ import annotations

from dokploy_wizard.verification import (
    ServiceVerificationResult,
    build_redacted_command_log,
    make_verification_result,
    redact_command,
    redact_text,
)


def test_service_verification_result_round_trips_with_optional_command() -> None:
    result = ServiceVerificationResult(
        service_name="litellm",
        tier="app",
        status="pass",
        detail="Readiness endpoint returned 200.",
        evidence_command="curl -fsS http://litellm:4000/health/readiness",
    )

    payload = result.to_dict()

    assert payload == {
        "detail": "Readiness endpoint returned 200.",
        "evidence_command": "curl -fsS http://litellm:4000/health/readiness",
        "service_name": "litellm",
        "status": "pass",
        "tier": "app",
    }
    assert ServiceVerificationResult.from_dict(payload) == result


def test_make_verification_result_redacts_detail_and_command() -> None:
    result = make_verification_result(
        service_name="openclaw",
        tier="downstream",
        passed=False,
        detail=(
            "Authorization: Bearer sk-super-secret failed while using "
            "OPENCLAW_GATEWAY_PASSWORD=hunter2"
        ),
        evidence_command=(
            "curl",
            "-H",
            "Authorization: Bearer sk-super-secret",
            "https://openclaw.example.com/health",
        ),
    )

    assert result.status == "fail"
    assert result.passed is False
    assert "sk-super-secret" not in result.detail
    assert "hunter2" not in result.detail
    assert "<REDACTED>" in result.detail
    assert result.evidence_command is not None
    assert "sk-super-secret" not in result.evidence_command
    assert "<REDACTED>" in result.evidence_command


def test_redacted_command_log_redacts_command_stdout_and_stderr() -> None:
    log = build_redacted_command_log(
        command=(
            "docker",
            "run",
            "-e",
            "API_KEY=sk-abc123",
            "-e",
            "PASSWORD=hunter2",
            "curlimages/curl:8.7.1",
        ),
        stdout='{"virtual_key":"sk-virtual-123"}',
        stderr="x-api-key: tskey-auth-123",
        returncode=7,
    )

    assert log.returncode == 7
    assert log.stdout is not None
    assert log.stderr is not None
    assert "sk-abc123" not in log.command
    assert "hunter2" not in log.command
    assert "sk-virtual-123" not in log.stdout
    assert "tskey-auth-123" not in log.stderr
    assert log.to_dict() == {
        "command": log.command,
        "stdout": log.stdout,
        "stderr": log.stderr,
        "returncode": 7,
    }


def test_redact_text_handles_query_params_and_json_style_fields() -> None:
    raw = (
        'https://example.test/health?token=abc123&ok=true '
        'payload={"api_key":"sk-json-123","virtual_key":"sk-virtual-456"}'
    )

    redacted = redact_text(raw)

    assert "abc123" not in redacted
    assert "sk-json-123" not in redacted
    assert "sk-virtual-456" not in redacted
    assert redacted.count("<REDACTED>") >= 3


def test_redact_command_supports_pre_rendered_shell_text() -> None:
    command = "curl -H 'Authorization: Bearer sk-inline-123' https://example.test"

    redacted = redact_command(command)

    assert "sk-inline-123" not in redacted
    assert "<REDACTED>" in redacted
