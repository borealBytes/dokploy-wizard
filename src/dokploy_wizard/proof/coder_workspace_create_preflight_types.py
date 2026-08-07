"""Closed types for Coder workspace-create preflight evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from dokploy_wizard.proof.model_sync_artifacts import JsonValue

UrlStatus = Literal["not_checked", "reachable", "unavailable"]
TokenStatus = Literal["not_checked", "issued", "unavailable"]
AuthStatus = Literal["not_checked", "authenticated", "unavailable"]
VersionHealth = Literal["not_checked", "healthy", "unhealthy", "unavailable"]
PresetSelection = Literal[
    "not_checked", "not_required", "automatic_default", "required", "invalid", "unavailable"
]
ExternalAuthStatus = Literal[
    "not_checked", "not_required", "satisfied", "unsatisfied", "unavailable"
]
PreflightBlocker = Literal[
    "target_template_missing",
    "target_template_ambiguous",
    "target_organization_ambiguous",
    "coder_url_unavailable",
    "coder_token_unavailable",
    "coder_auth_unavailable",
    "preflight_env_unavailable",
    "preflight_task1_context_unavailable",
    "preflight_transport_unavailable",
    "preflight_setup_unexpected",
    "preflight_transport_configuration_invalid",
    "coder_container_missing",
    "coder_container_not_running",
    "coder_container_restarting",
    "coder_container_ambiguous",
    "coder_container_discovery_unavailable",
    "coder_container_discovery_inconsistent",
    "coder_container_inspect_unavailable",
    "coder_shared_network_unavailable",
    "coder_shared_address_invalid",
    "active_template_version_unavailable",
    "active_template_version_unhealthy",
    "preset_selection_required",
    "preset_selection_invalid",
    "required_parameter_default_gap",
    "required_external_auth_unsatisfied",
    "preflight_payload_invalid",
]


class CoderCreatePreflightError(ValueError):
    """Raised when a Coder read-only response cannot be safely projected."""


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    template_id: str
    organization_id: str
    name: str
    active_version_id: str | None


@dataclass(frozen=True, slots=True)
class TemplateVersionFacts:
    required_parameter_default_gap_count: int
    required_external_auth_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class CoderCreatePreflightReport:
    url_status: UrlStatus
    token_status: TokenStatus
    auth_status: AuthStatus
    target_template_count: int
    organization_count: int
    target_organization_count: int
    active_template_version_health: VersionHealth
    preset_selection: PresetSelection
    required_parameter_default_gap_count: int
    required_external_auth_unsatisfied_count: int
    external_auth_status: ExternalAuthStatus
    blockers: tuple[PreflightBlocker, ...]

    def to_bytes(self) -> bytes:
        payload: dict[str, JsonValue] = {
            "active_template_version_health": self.active_template_version_health,
            "auth": self.auth_status,
            "blockers": list(self.blockers),
            "external_auth_status": self.external_auth_status,
            "organization_count": self.organization_count,
            "preset_selection": self.preset_selection,
            "required_external_auth_unsatisfied_count": (
                self.required_external_auth_unsatisfied_count
            ),
            "required_parameter_default_gap_count": self.required_parameter_default_gap_count,
            "schema_version": 1,
            "target_organization_count": self.target_organization_count,
            "target_template_count": self.target_template_count,
            "token": self.token_status,
            "url": self.url_status,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"

    @classmethod
    def from_bytes(cls, content: bytes) -> "CoderCreatePreflightReport":
        """Parse only the exact sanitized wire schema produced by this diagnostic."""

        try:
            value: JsonValue = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoderCreatePreflightError("preflight report is invalid") from error
        if not isinstance(value, dict) or set(value) != {
            "active_template_version_health",
            "auth",
            "blockers",
            "external_auth_status",
            "organization_count",
            "preset_selection",
            "required_external_auth_unsatisfied_count",
            "required_parameter_default_gap_count",
            "schema_version",
            "target_organization_count",
            "target_template_count",
            "token",
            "url",
        }:
            raise CoderCreatePreflightError("preflight report schema is invalid")
        if isinstance(value["schema_version"], bool) or value["schema_version"] != 1:
            raise CoderCreatePreflightError("preflight report schema is invalid")
        report = cls(
            url_status=_url_status(value["url"]),
            token_status=_token_status(value["token"]),
            auth_status=_auth_status(value["auth"]),
            target_template_count=_count(value["target_template_count"]),
            organization_count=_count(value["organization_count"]),
            target_organization_count=_count(value["target_organization_count"]),
            active_template_version_health=_version_health(value["active_template_version_health"]),
            preset_selection=_preset_selection(value["preset_selection"]),
            required_parameter_default_gap_count=_count(
                value["required_parameter_default_gap_count"]
            ),
            required_external_auth_unsatisfied_count=_count(
                value["required_external_auth_unsatisfied_count"]
            ),
            external_auth_status=_external_auth_status(value["external_auth_status"]),
            blockers=_blockers(value["blockers"]),
        )
        if content != report.to_bytes():
            raise CoderCreatePreflightError("preflight report bytes are not canonical")
        return report


def _url_status(value: JsonValue) -> UrlStatus:
    match value:
        case "not_checked" | "reachable" | "unavailable" as status:
            return status
        case _:
            raise CoderCreatePreflightError("preflight report URL is invalid")


def _token_status(value: JsonValue) -> TokenStatus:
    match value:
        case "not_checked" | "issued" | "unavailable" as status:
            return status
        case _:
            raise CoderCreatePreflightError("preflight report token is invalid")


def _auth_status(value: JsonValue) -> AuthStatus:
    match value:
        case "not_checked" | "authenticated" | "unavailable" as status:
            return status
        case _:
            raise CoderCreatePreflightError("preflight report auth is invalid")


def _version_health(value: JsonValue) -> VersionHealth:
    match value:
        case "not_checked" | "healthy" | "unhealthy" | "unavailable" as health:
            return health
        case _:
            raise CoderCreatePreflightError("preflight report version health is invalid")


def _preset_selection(value: JsonValue) -> PresetSelection:
    match value:
        case (
            (
                "not_checked"
                | "not_required"
                | "automatic_default"
                | "required"
                | "invalid"
                | "unavailable"
            ) as selection
        ):
            return selection
        case _:
            raise CoderCreatePreflightError("preflight report preset selection is invalid")


def _external_auth_status(value: JsonValue) -> ExternalAuthStatus:
    match value:
        case "not_checked" | "not_required" | "satisfied" | "unsatisfied" | "unavailable" as status:
            return status
        case _:
            raise CoderCreatePreflightError("preflight report external auth is invalid")


def _count(value: JsonValue) -> int:
    match value:
        case bool():
            raise CoderCreatePreflightError("preflight report count is invalid")
        case int() as count if count >= 0:
            return count
        case _:
            raise CoderCreatePreflightError("preflight report count is invalid")


def _blockers(value: JsonValue) -> tuple[PreflightBlocker, ...]:
    match value:
        case list() as blockers:
            return tuple(_blocker(blocker) for blocker in blockers)
        case _:
            raise CoderCreatePreflightError("preflight report blockers are invalid")


def _blocker(value: JsonValue) -> PreflightBlocker:
    match value:
        case (
            (
                "target_template_missing"
                | "target_template_ambiguous"
                | "target_organization_ambiguous"
            ) as blocker
        ):
            return blocker
        case (
            (
                "coder_url_unavailable" | "coder_token_unavailable" | "coder_auth_unavailable"
            ) as blocker
        ):
            return blocker
        case (
            (
                "preflight_transport_configuration_invalid"
                | "preflight_env_unavailable"
                | "preflight_task1_context_unavailable"
                | "preflight_transport_unavailable"
                | "preflight_setup_unexpected"
                | "coder_container_missing"
                | "coder_container_not_running"
                | "coder_container_restarting"
                | "coder_container_ambiguous"
                | "coder_container_discovery_unavailable"
                | "coder_container_discovery_inconsistent"
                | "coder_container_inspect_unavailable"
                | "coder_shared_network_unavailable"
                | "coder_shared_address_invalid"
                | "active_template_version_unavailable"
                | "active_template_version_unhealthy"
            ) as blocker
        ):
            return blocker
        case (
            (
                "preset_selection_required"
                | "preset_selection_invalid"
                | "required_parameter_default_gap"
            ) as blocker
        ):
            return blocker
        case "required_external_auth_unsatisfied" | "preflight_payload_invalid" as blocker:
            return blocker
        case _:
            raise CoderCreatePreflightError("preflight report blocker is invalid")
