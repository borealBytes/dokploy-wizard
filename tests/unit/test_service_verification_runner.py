from __future__ import annotations

import json

from dokploy_wizard.service_verification_runner import (
    ServiceVerificationCheck,
    run_service_verification_checks,
)
from dokploy_wizard.verification import make_verification_result


def test_service_verification_runner_passes_all_enabled_services() -> None:
    checks = (
        ServiceVerificationCheck(
            service_id="shared-core",
            label="Shared Core / LiteLLM",
            verify=lambda: make_verification_result(
                service_name="wizard-shared",
                tier="downstream",
                passed=True,
                detail="Shared Core is ready.",
            ),
        ),
        ServiceVerificationCheck(
            service_id="matrix",
            label="Matrix",
            verify=lambda: make_verification_result(
                service_name="wizard-matrix",
                tier="downstream",
                passed=True,
                detail="Matrix is ready.",
            ),
        ),
        ServiceVerificationCheck(
            service_id="coder",
            label="Coder",
            verify=lambda: make_verification_result(
                service_name="wizard-coder",
                tier="bootstrap",
                passed=True,
                detail="Coder is ready.",
            ),
        ),
        ServiceVerificationCheck(
            service_id="matrix",
            label="Matrix",
            verify=lambda: make_verification_result(
                service_name="wizard-matrix",
                tier="app",
                passed=True,
                detail="Matrix is ready.",
            ),
        ),
        ServiceVerificationCheck(
            service_id="docuseal",
            label="DocuSeal",
            verify=lambda: make_verification_result(
                service_name="wizard-docuseal",
                tier="app",
                passed=True,
                detail="DocuSeal is ready.",
            ),
        ),
        ServiceVerificationCheck(
            service_id="seaweedfs",
            label="SeaweedFS",
            verify=lambda: make_verification_result(
                service_name="wizard-seaweedfs",
                tier="app",
                passed=True,
                detail="SeaweedFS is ready.",
            ),
        ),
    )

    report = run_service_verification_checks(checks)
    payload = report.to_dict()

    assert report.passed is True
    assert payload["status"] == "pass"
    assert payload["summary"] == {"fail": 0, "pass": 6}
    assert [entry["service_id"] for entry in payload["entries"]] == [
        "shared-core",
        "matrix",
        "coder",
        "matrix",
        "docuseal",
        "seaweedfs",
    ]
    assert all(entry["status"] == "pass" for entry in payload["entries"])


def test_service_verification_runner_fails_with_redacted_detail() -> None:
    checks = (
        ServiceVerificationCheck(
            service_id="matrix",
            label="Matrix",
            verify=lambda: make_verification_result(
                service_name="wizard-matrix",
                tier="app",
                passed=False,
                detail=(
                    "MATRIX_CLIENT_SECRET=sk-secret-123 failed with "
                    "authorization: bearer sk-secret-123"
                ),
                evidence_command=[
                    "curl",
                    "-H",
                    "Authorization: Bearer sk-secret-123",
                    "https://matrix.example.com/health",
                ],
            ),
        ),
    )

    report = run_service_verification_checks(checks)
    payload = json.loads(json.dumps(report.to_dict()))
    entry = payload["entries"][0]

    assert report.passed is False
    assert payload["status"] == "fail"
    assert payload["summary"] == {"fail": 1, "pass": 0}
    assert entry["service_id"] == "matrix"
    assert entry["status"] == "fail"
    assert "sk-secret-123" not in entry["detail"]
    assert "<REDACTED>" in entry["detail"]
    assert "sk-secret-123" not in entry["evidence_command"]
