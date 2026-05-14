"""Service verification result contracts and redaction helpers."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

VerificationTier = Literal["app", "bootstrap", "downstream"]
VerificationStatus = Literal["pass", "fail"]

_VALID_TIERS = {"app", "bootstrap", "downstream"}
_VALID_STATUSES = {"pass", "fail"}
_SECRET_NAME_PATTERN = (
    r"(?:token|password|secret|api[_-]?key|access[_-]?key|secret[_-]?key|"
    r"virtual[_-]?key|ssh(?:[_-]?(?:key|pass(?:word)?))?|private[_-]?key)"
)
_KEY_VALUE_PATTERNS = (
    re.compile(
        rf'(?i)(\b[A-Z0-9_]*{_SECRET_NAME_PATTERN}[A-Z0-9_]*\b\s*[=:]\s*)([^\s,;]+)'
    ),
    re.compile(
        rf'(?i)(["\'][^"\']*{_SECRET_NAME_PATTERN}[^"\']*["\']\s*:\s*["\'])([^"\']+)(["\'])'
    ),
    re.compile(
        rf'(?i)([?&][^=\s&]*{_SECRET_NAME_PATTERN}[^=\s&]*=)([^&\s]+)'
    ),
)
_AUTH_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)(x-api-key\s*[:=]\s*)(\S+)"),
)
_TOKEN_VALUE_PATTERN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]+|tskey-[A-Za-z0-9_\-]+|gh[pousr]_[A-Za-z0-9_\-]+|"
    r"github_pat_[A-Za-z0-9_\-]+)\b"
)
_WIZARD_ENV_MARKER_PREFIX = "# dokploy-wizard-env"
_REDACTION_VALUE = "<REDACTED>"


@dataclass(frozen=True)
class RedactedCommandLog:
    command: str
    stdout: str | None = None
    stderr: str | None = None
    returncode: int | None = None

    def __post_init__(self) -> None:
        if self.command == "":
            msg = "Redacted command log command cannot be empty."
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"command": self.command}
        if self.stdout is not None:
            payload["stdout"] = self.stdout
        if self.stderr is not None:
            payload["stderr"] = self.stderr
        if self.returncode is not None:
            payload["returncode"] = self.returncode
        return payload


@dataclass(frozen=True)
class ServiceVerificationResult:
    service_name: str
    tier: VerificationTier
    status: VerificationStatus
    detail: str
    evidence_command: str | None = None

    def __post_init__(self) -> None:
        if self.service_name == "":
            msg = "Service verification result service_name cannot be empty."
            raise ValueError(msg)
        if self.tier not in _VALID_TIERS:
            msg = f"Unsupported verification tier {self.tier!r}."
            raise ValueError(msg)
        if self.status not in _VALID_STATUSES:
            msg = f"Unsupported verification status {self.status!r}."
            raise ValueError(msg)
        if self.detail == "":
            msg = "Service verification result detail cannot be empty."
            raise ValueError(msg)
        if self.evidence_command == "":
            msg = "Evidence command must be omitted or non-empty."
            raise ValueError(msg)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "detail": self.detail,
            "service_name": self.service_name,
            "status": self.status,
            "tier": self.tier,
        }
        if self.evidence_command is not None:
            payload["evidence_command"] = self.evidence_command
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ServiceVerificationResult:
        service_name = payload.get("service_name")
        tier = payload.get("tier")
        status = payload.get("status")
        detail = payload.get("detail")
        evidence_command = payload.get("evidence_command")
        if not isinstance(service_name, str) or service_name == "":
            raise ValueError("Expected non-empty string for 'service_name'.")
        if not isinstance(tier, str):
            raise ValueError("Expected string for 'tier'.")
        if not isinstance(status, str):
            raise ValueError("Expected string for 'status'.")
        if not isinstance(detail, str) or detail == "":
            raise ValueError("Expected non-empty string for 'detail'.")
        if evidence_command is not None and not isinstance(evidence_command, str):
            raise ValueError("Expected string for 'evidence_command'.")
        return cls(
            service_name=service_name,
            tier=cast(VerificationTier, tier),
            status=cast(VerificationStatus, status),
            detail=detail,
            evidence_command=evidence_command,
        )


def redact_text(value: str) -> str:
    """Replace token/password-like substrings with <REDACTED>."""

    redacted = redact_dokploy_env_payload(value)
    for pattern in _KEY_VALUE_PATTERNS:
        redacted = pattern.sub(_replace_secret_value, redacted)
    for pattern in _AUTH_PATTERNS:
        redacted = pattern.sub(r"\1<REDACTED>", redacted)
    return _TOKEN_VALUE_PATTERN.sub(_REDACTION_VALUE, redacted)


def redact_dokploy_env_payload(value: str) -> str:
    """Redact wizard-owned Dokploy env payload assignment values."""

    lines: list[str] = []
    redact_next_assignment = False
    for line in value.splitlines(keepends=True):
        line_body = line.removesuffix("\n").removesuffix("\r")
        line_ending = line[len(line_body) :]
        if redact_next_assignment and _looks_like_env_assignment(line_body):
            key = line_body.split("=", 1)[0]
            lines.append(f"{key}={_REDACTION_VALUE}{line_ending}")
            redact_next_assignment = False
            continue
        redact_next_assignment = line_body.startswith(_WIZARD_ENV_MARKER_PREFIX)
        lines.append(line)
    return "".join(lines)


def redact_data(value: Any) -> Any:
    """Recursively redact strings and secret-keyed mapping values."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key_is_sensitive(key):
                redacted[key] = _REDACTION_VALUE
            else:
                redacted[key] = redact_data(item)
        return redacted
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    return value


def redacted_env_spec_metadata(env_specs: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Return public env-spec metadata without raw values."""

    redacted: list[dict[str, Any]] = []
    for spec in env_specs:
        entry: dict[str, Any] = {
            "dokploy_scope": getattr(spec, "dokploy_scope"),
            "name": getattr(spec, "name"),
            "owner": getattr(spec, "owner"),
            "ownership_marker": getattr(spec, "ownership_marker"),
            "redacted_fingerprint": getattr(spec, "redacted_fingerprint"),
            "required": getattr(spec, "required"),
            "sensitive": getattr(spec, "sensitive"),
            "source": getattr(spec, "source"),
            "target_services": list(getattr(spec, "target_services")),
        }
        placeholder = getattr(spec, "placeholder")
        if placeholder is not None:
            entry["placeholder"] = placeholder
        redacted.append(entry)
    return tuple(redacted)


def key_is_sensitive(key: str) -> bool:
    return re.search(_SECRET_NAME_PATTERN, key, flags=re.IGNORECASE) is not None


def redact_command(command: Sequence[str] | str) -> str:
    rendered = command if isinstance(command, str) else shlex.join(command)
    return redact_text(rendered)


def build_redacted_command_log(
    *,
    command: Sequence[str] | str,
    stdout: str | None = None,
    stderr: str | None = None,
    returncode: int | None = None,
) -> RedactedCommandLog:
    return RedactedCommandLog(
        command=redact_command(command),
        stdout=None if stdout is None else redact_text(stdout),
        stderr=None if stderr is None else redact_text(stderr),
        returncode=returncode,
    )


def make_verification_result(
    *,
    service_name: str,
    tier: VerificationTier,
    passed: bool,
    detail: str,
    evidence_command: Sequence[str] | str | None = None,
) -> ServiceVerificationResult:
    return ServiceVerificationResult(
        service_name=service_name,
        tier=tier,
        status="pass" if passed else "fail",
        detail=redact_text(detail),
        evidence_command=None if evidence_command is None else redact_command(evidence_command),
    )


def _replace_secret_value(match: re.Match[str]) -> str:
    suffix = match.group(3) if match.lastindex and match.lastindex >= 3 else ""
    return f"{match.group(1)}{_REDACTION_VALUE}{suffix}"


def _looks_like_env_assignment(line: str) -> bool:
    if line == "" or line.lstrip().startswith("#") or "=" not in line:
        return False
    key = line.split("=", 1)[0]
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key) is not None
