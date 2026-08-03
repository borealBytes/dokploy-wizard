from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

import dokploy_wizard.dokploy.task1_coder_secret_attestation as attestation
from dokploy_wizard.state.sync_schema import JsonValue


def _payload() -> JsonValue:
    payload: JsonValue = json.loads(
        files("dokploy_wizard.dokploy")
        .joinpath("task1_coder_secret_lock.json")
        .read_text(encoding="utf-8")
    )
    return payload


def _record(records: list[JsonValue], index: int) -> dict[str, JsonValue]:
    value = records[index]
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    "tamper",
    ("provenance", "lifecycle", "inventory", "order", "duplicate", "uuid", "unknown_field"),
)
def test_task1_lock_tampering_fails_closed_without_echoing_lock_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    value = _payload()
    assert isinstance(value, dict)
    payload = value
    records_value = payload["records"]
    assert isinstance(records_value, list)
    records = records_value

    match tamper:
        case "provenance":
            payload["proof_commit"] = "0" * 40
        case "lifecycle":
            payload["post_install_phase"] = "unknown"
        case "inventory":
            _record(records, 0)["description"] = "tampered metadata"
        case "order":
            payload["records"] = list(reversed(records))
        case "duplicate":
            _record(records, 1)["secret_id"] = _record(records, 0)["secret_id"]
        case "uuid":
            _record(records, 0)["secret_id"] = "not-a-uuid"
        case "unknown_field":
            payload["unexpected"] = "field"
        case _:
            raise AssertionError("test case is invalid")

    (tmp_path / "task1_coder_secret_lock.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setattr(attestation, "files", lambda _package: tmp_path)

    with pytest.raises(ValueError) as raised:
        attestation.proves_task1_created_secret(
            secret_id="00000000-0000-0000-0000-000000000000",
            name="test-name",
            env_name="TEST_ENV",
            description="test metadata",
        )

    assert "tampered metadata" not in str(raised.value)
