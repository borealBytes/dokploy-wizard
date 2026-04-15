"""State-directory loading and persistence helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from dokploy_wizard.state.models import (
    AppliedStateCheckpoint,
    DesiredState,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
)

RAW_INPUT_FILE = "raw-input.json"
DESIRED_STATE_FILE = "desired-state.json"
APPLIED_STATE_FILE = "applied-state.json"
OWNERSHIP_LEDGER_FILE = "ownership-ledger.json"
STATE_DOCUMENT_FILES = (
    RAW_INPUT_FILE,
    DESIRED_STATE_FILE,
    APPLIED_STATE_FILE,
    OWNERSHIP_LEDGER_FILE,
)

_DocumentT = TypeVar("_DocumentT")


@dataclass(frozen=True)
class LoadedState:
    raw_input: RawEnvInput | None
    desired_state: DesiredState | None
    applied_state: AppliedStateCheckpoint | None
    ownership_ledger: OwnershipLedger | None


def load_state_dir(state_dir: Path) -> LoadedState:
    return LoadedState(
        raw_input=_load_optional_document(state_dir / RAW_INPUT_FILE, RawEnvInput.from_dict),
        desired_state=_load_optional_document(
            state_dir / DESIRED_STATE_FILE, DesiredState.from_dict
        ),
        applied_state=_load_optional_document(
            state_dir / APPLIED_STATE_FILE,
            AppliedStateCheckpoint.from_dict,
        ),
        ownership_ledger=_load_optional_document(
            state_dir / OWNERSHIP_LEDGER_FILE,
            OwnershipLedger.from_dict,
        ),
    )


def write_inspection_snapshot(
    state_dir: Path, raw_input: RawEnvInput, desired_state_snapshot: dict[str, Any]
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / RAW_INPUT_FILE, raw_input.to_dict())
    _write_document(state_dir / DESIRED_STATE_FILE, desired_state_snapshot)


def validate_install_state(loaded_state: LoadedState, desired_state: DesiredState) -> bool:
    """Validate existing install state and report whether it already exists."""

    _validate_state_document_set(loaded_state)

    if loaded_state.desired_state is None:
        return False
    if loaded_state.applied_state is None:
        return False

    if loaded_state.desired_state.to_dict() != desired_state.to_dict():
        msg = "Existing desired state does not match this install request."
        raise StateValidationError(msg)

    if loaded_state.applied_state.desired_state_fingerprint != desired_state.fingerprint():
        msg = "Existing applied state fingerprint does not match the desired state."
        raise StateValidationError(msg)

    return True


def validate_existing_state(loaded_state: LoadedState) -> bool:
    """Validate the current state-dir document set without requiring a matching target."""

    _validate_state_document_set(loaded_state)
    return loaded_state.desired_state is not None


def write_target_state(
    state_dir: Path, raw_input: RawEnvInput, desired_state: DesiredState
) -> None:
    """Persist the requested raw input and desired state before mutating phases."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / RAW_INPUT_FILE, raw_input.to_dict())
    _write_document(state_dir / DESIRED_STATE_FILE, desired_state.to_dict())


def _validate_state_document_set(loaded_state: LoadedState) -> None:
    """Validate all-or-none state documents plus supported checkpoint step names."""

    documents_present = {
        "raw input": loaded_state.raw_input is not None,
        "desired state": loaded_state.desired_state is not None,
        "applied state": loaded_state.applied_state is not None,
        "ownership ledger": loaded_state.ownership_ledger is not None,
    }
    present_count = sum(documents_present.values())
    if present_count == 0:
        return
    if present_count != len(documents_present):
        missing = sorted(name for name, present in documents_present.items() if not present)
        msg = (
            "Invalid existing state: expected raw input, desired state, applied state, "
            f"and ownership ledger together; missing {', '.join(missing)}."
        )
        raise StateValidationError(msg)

    assert loaded_state.applied_state is not None

    allowed_steps = {
        "preflight",
        "dokploy_bootstrap",
        "tailscale",
        "networking",
        "cloudflare_access",
        "shared_core",
        "headscale",
        "matrix",
        "nextcloud",
        "seaweedfs",
        "openclaw",
        "my-farm-advisor",
    }
    unexpected_steps = sorted(
        step for step in loaded_state.applied_state.completed_steps if step not in allowed_steps
    )
    if unexpected_steps:
        msg = f"Existing applied state contains unsupported completed steps: {unexpected_steps}."
        raise StateValidationError(msg)


def persist_install_scaffold(
    state_dir: Path, raw_input: RawEnvInput, desired_state: DesiredState
) -> None:
    """Persist the initial Task 3 state scaffold before bootstrap mutation."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / RAW_INPUT_FILE, raw_input.to_dict())
    _write_document(state_dir / DESIRED_STATE_FILE, desired_state.to_dict())
    _write_document(
        state_dir / APPLIED_STATE_FILE,
        AppliedStateCheckpoint(
            format_version=desired_state.format_version,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=(),
        ).to_dict(),
    )
    _write_document(
        state_dir / OWNERSHIP_LEDGER_FILE,
        OwnershipLedger(format_version=desired_state.format_version, resources=()).to_dict(),
    )


def write_applied_checkpoint(state_dir: Path, applied_state: AppliedStateCheckpoint) -> None:
    """Persist an updated applied-state checkpoint."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / APPLIED_STATE_FILE, applied_state.to_dict())


def write_ownership_ledger(state_dir: Path, ownership_ledger: OwnershipLedger) -> None:
    """Persist an updated ownership ledger."""

    state_dir.mkdir(parents=True, exist_ok=True)
    _write_document(state_dir / OWNERSHIP_LEDGER_FILE, ownership_ledger.to_dict())


def clear_state_documents(state_dir: Path) -> None:
    """Remove all persisted state documents together after full teardown."""

    for file_name in STATE_DOCUMENT_FILES:
        document_path = state_dir / file_name
        if document_path.exists():
            document_path.unlink()


def _load_optional_document(
    path: Path,
    loader: Callable[[dict[str, Any]], _DocumentT],
) -> _DocumentT | None:
    if not path.exists():
        return None
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        msg = f"State file '{path.name}' must contain a JSON object."
        raise StateValidationError(msg)
    try:
        return loader(payload)
    except StateValidationError as error:
        msg = f"Invalid state file '{path.name}': {error}"
        raise StateValidationError(msg) from error


def _read_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as error:
        msg = f"State file '{path.name}' contains invalid JSON: {error.msg}."
        raise StateValidationError(msg) from error


def _write_document(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
