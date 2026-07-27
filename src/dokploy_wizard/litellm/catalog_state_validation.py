from __future__ import annotations

import re
from datetime import datetime
from typing import assert_never

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.catalog_missing import MissingState
from dokploy_wizard.litellm.catalog_retirement import AnomalyState
from dokploy_wizard.litellm.catalog_sources import OFFICIAL_SOURCE
from dokploy_wizard.litellm.catalog_state_types import ResultStatus, RuntimeState
from dokploy_wizard.litellm.catalog_types import (
    ObservationStatus,
    QuarantineReason,
    SourceName,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StateRecordError(ValueError):
    pass


def mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise StateRecordError(f"{label} must be an object")
    return value


def sequence(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise StateRecordError(f"{label} must be a list")
    return value


def require_keys(value: dict[str, JsonValue], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise StateRecordError(f"{label} keys are invalid")


def ids(value: JsonValue, label: str) -> tuple[str, ...]:
    result = tuple(text(item, label) for item in sequence(value, label))
    if result != tuple(sorted(set(result))):
        raise StateRecordError(f"{label} must be sorted and unique")
    return result


def text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateRecordError(f"{label} must be text")
    return value


def optional_text(value: JsonValue, label: str) -> str | None:
    return None if value is None else text(value, label)


def integer(value: JsonValue, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateRecordError(f"{label} must be a nonnegative integer")
    return value


def positive_integer(value: JsonValue, label: str) -> int:
    result = integer(value, label)
    if result == 0:
        raise StateRecordError(f"{label} must be positive")
    return result


def optional_integer(value: JsonValue, label: str) -> int | None:
    return None if value is None else integer(value, label)


def boolean(value: JsonValue, label: str) -> bool:
    if not isinstance(value, bool):
        raise StateRecordError(f"{label} must be boolean")
    return value


def digest(value: JsonValue) -> str:
    result = text(value, "sha256")
    if _SHA256.fullmatch(result) is None:
        raise StateRecordError("sha256 is invalid")
    return result


def optional_digest(value: JsonValue) -> str | None:
    return None if value is None else digest(value)


def timestamp(value: JsonValue, label: str) -> datetime:
    raw = text(value, label)
    if not raw.endswith("Z"):
        raise StateRecordError(f"{label} must be RFC3339 UTC")
    try:
        result = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise StateRecordError(f"{label} must be RFC3339 UTC") from error
    if result.isoformat().replace("+00:00", "Z") != raw:
        raise StateRecordError(f"{label} must be canonical RFC3339 UTC")
    return result


def optional_timestamp(value: JsonValue, label: str) -> datetime | None:
    return None if value is None else timestamp(value, label)


def observation_status(value: str) -> ObservationStatus:
    if value == "accepted":
        return "accepted"
    if value == "accepted_with_quarantine":
        return "accepted_with_quarantine"
    if value == "rejected_invalid":
        return "rejected_invalid"
    if value == "quarantined_anomalous":
        return "quarantined_anomalous"
    if value == "rejected_clock_regression":
        return "rejected_clock_regression"
    raise StateRecordError("observation status is invalid")


def anomaly_state(value: str) -> AnomalyState:
    if value == "none":
        return "none"
    if value == "pending":
        return "pending"
    if value == "quarantined":
        return "quarantined"
    if value == "confirmed":
        return "confirmed"
    if value == "cleared":
        return "cleared"
    raise StateRecordError("anomaly state is invalid")


def missing_state(value: str) -> MissingState:
    if value == "present":
        return "present"
    if value == "pending_absence":
        return "pending_absence"
    if value == "confirmed_absence":
        return "confirmed_absence"
    if value == "eligible_for_delete":
        return "eligible_for_delete"
    if value == "deleted":
        return "deleted"
    if value == "reappeared":
        return "reappeared"
    raise StateRecordError("missing state is invalid")


def source_name(value: str) -> SourceName:
    if value == "zen":
        return "zen"
    if value == "models_dev":
        return "models_dev"
    if value == "official":
        return "official"
    raise StateRecordError("provenance source is invalid")


def validate_source_provenance(
    source: SourceName,
    commit: str | None,
    blob: str | None,
) -> None:
    match source:
        case "official":
            if commit != OFFICIAL_SOURCE.commit or blob != OFFICIAL_SOURCE.blob:
                raise StateRecordError("official provenance does not match the source pin")
        case "zen" | "models_dev":
            if commit is not None or blob is not None:
                raise StateRecordError("mutable source provenance must not include commit or blob")
        case unreachable:
            assert_never(unreachable)


def quarantine_reason(value: str) -> QuarantineReason:
    if value == "missing_official_transport":
        return "missing_official_transport"
    if value == "invalid_pricing":
        return "invalid_pricing"
    if value == "invalid_limits":
        return "invalid_limits"
    raise StateRecordError("quarantine reason is invalid")


def result_status(value: str) -> ResultStatus:
    if value == "success":
        return "success"
    if value == "success_with_quarantine":
        return "success_with_quarantine"
    if value == "no_change":
        return "no_change"
    if value == "quarantined":
        return "quarantined"
    if value == "failed":
        return "failed"
    raise StateRecordError("result status is invalid")


def runtime_state(value: str) -> RuntimeState:
    if value == "enabled":
        return "enabled"
    if value == "disabled":
        return "disabled"
    if value == "blocked":
        return "blocked"
    raise StateRecordError("runtime state is invalid")
