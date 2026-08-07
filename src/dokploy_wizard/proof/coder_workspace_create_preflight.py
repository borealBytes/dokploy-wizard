"""Closed-schema classification for a read-only Coder create preflight."""

from __future__ import annotations

from typing import Final

from dokploy_wizard.proof.coder_workspace_create_preflight_types import (
    CoderCreatePreflightError,
    CoderCreatePreflightReport,
    ExternalAuthStatus,
    PreflightBlocker,
    TemplateRecord,
)
from dokploy_wizard.proof.model_sync_artifacts import JsonValue

_TARGET_TEMPLATE: Final = "ubuntu-vscode-opencode-pi"



def classify_preflight(
    templates: JsonValue,
    version: JsonValue | None,
    parameters: JsonValue | None,
    presets: JsonValue | None,
    external_auth: JsonValue | None,
) -> CoderCreatePreflightReport:
    """Classify create prerequisites without exposing Coder response values."""

    records = _template_records(templates)
    targets = tuple(record for record in records if record.name == _TARGET_TEMPLATE)
    organizations = frozenset(record.organization_id for record in records)
    target_organizations = frozenset(record.organization_id for record in targets)
    if len(targets) == 0:
        return _target_report(
            target_template_count=0,
            organization_count=len(organizations),
            target_organization_count=0,
            blockers=("target_template_missing",),
        )
    if len(targets) != 1:
        return _target_report(
            target_template_count=len(targets),
            organization_count=len(organizations),
            target_organization_count=len(target_organizations),
            blockers=("target_template_ambiguous", "target_organization_ambiguous"),
        )
    active_version_id = targets[0].active_version_id
    if (
        active_version_id is None
        or version is None
        or parameters is None
        or presets is None
        or external_auth is None
    ):
        report = _target_report(
            target_template_count=1,
            organization_count=len(organizations),
            target_organization_count=1,
            blockers=("active_template_version_unavailable",),
        )
        return CoderCreatePreflightReport(
            url_status=report.url_status,
            token_status=report.token_status,
            auth_status=report.auth_status,
            target_template_count=report.target_template_count,
            organization_count=report.organization_count,
            target_organization_count=report.target_organization_count,
            active_template_version_health="unavailable",
            preset_selection="unavailable",
            required_parameter_default_gap_count=0,
            required_external_auth_unsatisfied_count=0,
            external_auth_status="unavailable",
            blockers=report.blockers,
        )
    try:
        _version_facts(version, active_version_id)
        preset_count = _preset_count(presets)
        parameter_gap_count = _required_parameter_gap_count(parameters)
        unsatisfied_external_auth_count = _unsatisfied_external_auth_count(external_auth)
    except CoderCreatePreflightError:
        return _target_report(
            target_template_count=1,
            organization_count=len(organizations),
            target_organization_count=1,
            blockers=("preflight_payload_invalid",),
        )
    blockers: list[PreflightBlocker] = []
    if preset_count > 0:
        blockers.append("preset_selection_required")
    if parameter_gap_count > 0:
        blockers.append("required_parameter_default_gap")
    if unsatisfied_external_auth_count > 0:
        blockers.append("required_external_auth_unsatisfied")
    external_auth_status: ExternalAuthStatus = (
        "not_required"
        if unsatisfied_external_auth_count == 0 and not _has_required_external_auth(external_auth)
        else "satisfied"
        if unsatisfied_external_auth_count == 0
        else "unsatisfied"
    )
    return CoderCreatePreflightReport(
        url_status="not_checked",
        token_status="not_checked",
        auth_status="not_checked",
        target_template_count=1,
        organization_count=len(organizations),
        target_organization_count=1,
        active_template_version_health="healthy",
        preset_selection="required" if preset_count > 0 else "not_required",
        required_parameter_default_gap_count=parameter_gap_count,
        required_external_auth_unsatisfied_count=unsatisfied_external_auth_count,
        external_auth_status=external_auth_status,
        blockers=tuple(blockers),
    )


def target_active_version_id(templates: JsonValue) -> str | None:
    """Return one target active-version ID only when its identity is unambiguous."""

    targets = tuple(
        record for record in _template_records(templates) if record.name == _TARGET_TEMPLATE
    )
    if len(targets) != 1:
        return None
    return targets[0].active_version_id


def _target_report(
    *,
    target_template_count: int,
    organization_count: int,
    target_organization_count: int,
    blockers: tuple[PreflightBlocker, ...],
) -> CoderCreatePreflightReport:
    return CoderCreatePreflightReport(
        url_status="not_checked",
        token_status="not_checked",
        auth_status="not_checked",
        target_template_count=target_template_count,
        organization_count=organization_count,
        target_organization_count=target_organization_count,
        active_template_version_health="not_checked",
        preset_selection="not_checked",
        required_parameter_default_gap_count=0,
        required_external_auth_unsatisfied_count=0,
        external_auth_status="not_checked",
        blockers=blockers,
    )


def _template_records(value: JsonValue) -> tuple[TemplateRecord, ...]:
    return tuple(_template_record(item) for item in _records(value, "templates"))


def _template_record(value: JsonValue) -> TemplateRecord:
    row = _mapping(value, "template")
    template = _mapping(row.get("Template", row), "template")
    organization = row.get("Organization")
    organization_id = (
        _text(template.get("organization_id"), "template.organization_id")
        if organization is None
        else _text(_mapping(organization, "template organization").get("id"), "organization.id")
    )
    _text(template.get("id"), "template.id")
    return TemplateRecord(
        organization_id=organization_id,
        name=_text(template.get("name"), "template.name"),
        active_version_id=_optional_text(template.get("active_version_id")),
    )


def _version_facts(value: JsonValue, expected_id: str) -> None:
    version = _mapping(value, "template version")
    if _text(version.get("id"), "template version.id") != expected_id:
        raise CoderCreatePreflightError("template version identity is invalid")


def _parameter_requires_input(value: JsonValue) -> bool:
    parameter = _mapping(value, "template parameter")
    _text(parameter.get("default_value"), "template parameter.default_value")
    return _bool(parameter.get("required"), "template parameter.required")


def _required_parameter_gap_count(value: JsonValue) -> int:
    return sum(
        1
        for parameter in _records(value, "template version rich parameters")
        if _parameter_requires_input(parameter)
    )


def _unsatisfied_external_auth_count(value: JsonValue) -> int:
    return sum(
        1
        for record in (_mapping(item, "external auth") for item in _records(value, "external auth"))
        if _required_external_auth(record)
        and not _bool(record.get("authenticated"), "external auth.authenticated")
    )


def _has_required_external_auth(value: JsonValue) -> bool:
    return any(
        _required_external_auth(record)
        for record in (_mapping(item, "external auth") for item in _records(value, "external auth"))
    )


def _required_external_auth(record: dict[str, JsonValue]) -> bool:
    _text(record.get("id"), "external auth.id")
    return not _bool(record.get("optional"), "external auth.optional")


def _preset_count(value: JsonValue) -> int:
    presets = _records(value, "template version presets")
    for preset in presets:
        _text(_mapping(preset, "template version preset").get("ID"), "template version preset.ID")
    return len(presets)


def _records(value: JsonValue | None, label: str) -> list[JsonValue]:
    match value:
        case list() as records:
            return records
        case _:
            raise CoderCreatePreflightError(f"{label} is invalid")


def _mapping(value: JsonValue | None, label: str) -> dict[str, JsonValue]:
    match value:
        case dict() as mapping:
            return mapping
        case _:
            raise CoderCreatePreflightError(f"{label} is invalid")


def _text(value: JsonValue | None, label: str) -> str:
    match value:
        case str() as text:
            return text
        case _:
            raise CoderCreatePreflightError(f"{label} is invalid")


def _bool(value: JsonValue | None, label: str) -> bool:
    match value:
        case bool() as result:
            return result
        case _:
            raise CoderCreatePreflightError(f"{label} is invalid")


def _optional_text(value: JsonValue | None) -> str | None:
    match value:
        case None:
            return None
        case str() as text:
            return text
        case _:
            raise CoderCreatePreflightError("template.active_version_id is invalid")
