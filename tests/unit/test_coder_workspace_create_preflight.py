from __future__ import annotations

import pytest

from dokploy_wizard.proof.coder_workspace_create_preflight import classify_preflight
from dokploy_wizard.proof.coder_workspace_create_preflight_types import (
    CoderCreatePreflightError,
    CoderCreatePreflightReport,
)
from dokploy_wizard.proof.model_sync_artifacts import JsonValue


def test_preflight_blocks_missing_target_template() -> None:
    # Given
    templates: JsonValue = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "other-template",
            "organization_id": "00000000-0000-4000-8000-000000000011",
        }
    ]

    # When
    report = classify_preflight(templates, None, None, None, None)

    # Then
    assert report.target_template_count == 0
    assert report.blockers == ("target_template_missing",)


def test_preflight_blocks_ambiguous_target_template_without_retaining_identity() -> None:
    # Given
    templates: JsonValue = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "ubuntu-vscode-opencode-pi",
            "organization_id": "00000000-0000-4000-8000-000000000011",
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "name": "ubuntu-vscode-opencode-pi",
            "organization_id": "00000000-0000-4000-8000-000000000012",
        },
    ]

    # When
    report = classify_preflight(templates, None, None, None, None)

    # Then
    assert report.target_template_count == 2
    assert report.target_organization_count == 2
    assert report.blockers == ("target_template_ambiguous", "target_organization_ambiguous")
    assert "00000000" not in report.to_bytes().decode()


def test_preflight_blocks_noninteractive_contract_gaps() -> None:
    # Given
    templates: JsonValue = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "ubuntu-vscode-opencode-pi",
            "organization_id": "00000000-0000-4000-8000-000000000011",
            "active_version_id": "00000000-0000-4000-8000-000000000101",
            "use_classic_parameter_flow": True,
        }
    ]
    version: JsonValue = {
        "id": "00000000-0000-4000-8000-000000000101",
        "template_id": "00000000-0000-4000-8000-000000000001",
        "organization_id": "00000000-0000-4000-8000-000000000011",
        "archived": False,
        "job": {"status": "succeeded", "completed_at": "2026-08-07T00:00:00Z"},
    }
    parameters: JsonValue = [
        {
            "name": "required-without-default",
            "required": True,
            "default_value": "",
        }
    ]
    presets: JsonValue = [
        {"ID": "00000000-0000-4000-8000-000000000301", "Name": "default", "Default": False}
    ]
    external_auth: JsonValue = [
        {
            "id": "00000000-0000-4000-8000-000000000201",
            "authenticated": False,
            "optional": False,
        }
    ]

    # When
    report = classify_preflight(templates, version, parameters, presets, external_auth)

    # Then
    assert report.active_template_version_health == "healthy"
    assert report.preset_selection == "required"
    assert report.required_parameter_default_gap_count == 1
    assert report.required_external_auth_unsatisfied_count == 1
    assert report.blockers == (
        "preset_selection_required",
        "required_parameter_default_gap",
        "required_external_auth_unsatisfied",
    )
    payload = report.to_bytes().decode()
    assert "required-without-default" not in payload
    assert "00000000" not in payload


def test_preflight_blocks_required_parameter_even_with_default_value() -> None:
    # Given
    templates: JsonValue = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "ubuntu-vscode-opencode-pi",
            "organization_id": "00000000-0000-4000-8000-000000000011",
            "active_version_id": "00000000-0000-4000-8000-000000000101",
            "use_classic_parameter_flow": True,
        }
    ]
    version: JsonValue = {
        "id": "00000000-0000-4000-8000-000000000101",
        "template_id": "00000000-0000-4000-8000-000000000001",
        "organization_id": "00000000-0000-4000-8000-000000000011",
        "archived": False,
        "job": {"status": "succeeded", "completed_at": "2026-08-07T00:00:00Z"},
    }
    parameters: JsonValue = [{"name": "required", "required": True, "default_value": "value"}]

    # When
    report = classify_preflight(templates, version, parameters, [], [])

    # Then
    assert report.required_parameter_default_gap_count == 1
    assert report.blockers == ("required_parameter_default_gap",)


@pytest.mark.parametrize(
    ("presets", "selection", "blockers"),
    [
        (None, "not_required", ()),
        ([], "not_required", ()),
        ([{"ID": "id", "Name": "default", "Default": True}], "automatic_default", ()),
        (
            [{"ID": "id", "Name": "one", "Default": False}],
            "required",
            ("preset_selection_required",),
        ),
        (
            [
                {"ID": "one", "Name": "one", "Default": True},
                {"ID": "two", "Name": "two", "Default": True},
            ],
            "invalid",
            ("preset_selection_invalid",),
        ),
    ],
)
def test_preflight_classifies_coder_preset_default_selection(
    presets: JsonValue | None, selection: str, blockers: tuple[str, ...]
) -> None:
    # Given
    templates: JsonValue = [
        {
            "id": "template",
            "name": "ubuntu-vscode-opencode-pi",
            "organization_id": "org",
            "active_version_id": "version",
        }
    ]
    version: JsonValue = {
        "id": "version",
        "template_id": "template",
        "archived": False,
        "job": {"status": "succeeded", "completed_at": "done"},
    }

    # When
    report = classify_preflight(templates, version, [], presets, [])

    # Then
    assert report.preset_selection == selection
    assert report.blockers == blockers


@pytest.mark.parametrize(
    "version",
    [
        {
            "id": "wrong",
            "template_id": "template",
            "archived": False,
            "job": {"status": "succeeded", "completed_at": "done"},
        },
        {
            "id": "version",
            "template_id": "wrong",
            "archived": False,
            "job": {"status": "succeeded", "completed_at": "done"},
        },
        {
            "id": "version",
            "template_id": "template",
            "archived": True,
            "job": {"status": "succeeded", "completed_at": "done"},
        },
        {
            "id": "version",
            "template_id": "template",
            "archived": False,
            "job": {"status": "running", "completed_at": None},
        },
    ],
)
def test_preflight_blocks_unhealthy_active_template_version(version: JsonValue) -> None:
    # Given
    templates: JsonValue = [
        {
            "id": "template",
            "name": "ubuntu-vscode-opencode-pi",
            "organization_id": "org",
            "active_version_id": "version",
        }
    ]

    # When
    report = classify_preflight(templates, version, [], [], [])

    # Then
    assert report.active_template_version_health == "unhealthy"
    assert report.blockers == ("active_template_version_unhealthy",)


def test_report_rejects_unknown_schema_fields() -> None:
    # Given
    report = CoderCreatePreflightReport(
        url_status="reachable",
        token_status="issued",
        auth_status="authenticated",
        target_template_count=1,
        organization_count=1,
        target_organization_count=1,
        active_template_version_health="healthy",
        preset_selection="not_required",
        required_parameter_default_gap_count=0,
        required_external_auth_unsatisfied_count=0,
        external_auth_status="not_required",
        blockers=(),
    )

    # When / Then
    with pytest.raises(CoderCreatePreflightError, match="schema"):
        CoderCreatePreflightReport.from_bytes(
            report.to_bytes().removesuffix(b"\n").removesuffix(b"}") + b',"raw":"x"}'
        )
