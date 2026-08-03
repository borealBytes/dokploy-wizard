"""Typed interpretation of result-hash-verified Task 1 artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Protocol, assert_never

from dokploy_wizard import proof
from dokploy_wizard.proof.model_sync_lifecycle_schema import (
    SingleHostLifecycleReceipt,
    parse_lifecycle_receipt,
)
from dokploy_wizard.state.models import (
    OwnershipLedger,
    StateValidationError,
)
from dokploy_wizard.state.uninstall_authority_schema import UninstallAuthorityError
from dokploy_wizard.state.upgrade_io import canonical_bytes

_MAX_PREFLIGHT_BYTES: Final = 4 * 1024 * 1024
_MAX_BASELINE_BYTES: Final = 16 * 1024 * 1024
_MAX_LIFECYCLE_BYTES: Final = 256 * 1024
_MAX_LEDGER_BYTES: Final = 4 * 1024 * 1024
_SAFE_STATE_FILE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json")
_PREFLIGHT_KEYS: Final = {
    "architecture", "boot_sha256", "host_identities_distinct", "host_identity_mode",
    "inventory", "machine_sha256", "namespace_clean", "provenance_role",
    "schema_version", "ssh_sha256", "temporal_clean_epoch_evidence",
}
_BASELINE_KEYS: Final = {
    "cloudflare", "cloudflare_cleanup_receipt", "cloudflare_final_journal_sha256",
    "coder_secrets", "legacy_workspace_managed_fingerprints", "post_cleanup_cloudflare",
    "post_install_cloudflare", "pre_install_cloudflare", "preexisting_cloudflare",
    "resource_inventories", "stack_name", "templates", "workspaces",
}


class Task1ArtifactBundle(Protocol):
    @property
    def host_a_preflight_bytes(self) -> bytes: ...

    @property
    def baseline_bytes(self) -> bytes: ...

    @property
    def lifecycle_bytes(self) -> bytes: ...

    @property
    def ownership_ledger_bytes(self) -> bytes: ...


class Task1LegacyArtifactError(UninstallAuthorityError):
    """Raised when a hash-verified Task 1 artifact violates its typed schema."""


@dataclass(frozen=True, slots=True)
class ParsedTask1LegacyArtifacts:
    ownership_ledger: OwnershipLedger
    lifecycle: SingleHostLifecycleReceipt
    state_files: tuple[str, ...]


def parse_task1_legacy_artifacts(bundle: Task1ArtifactBundle) -> ParsedTask1LegacyArtifacts:
    """Parse the three verified artifacts and their canonical ledger binding."""

    preflight = _parse_preflight(
        proof_document(bundle.host_a_preflight_bytes, _MAX_PREFLIGHT_BYTES, "Host A preflight")
    )
    ledger = _parse_ledger(bundle.ownership_ledger_bytes)
    state_files = _parse_baseline(
        proof_document(bundle.baseline_bytes, _MAX_BASELINE_BYTES, "baseline"),
        bundle.ownership_ledger_bytes,
    )
    try:
        lifecycle = parse_lifecycle_receipt(
            proof_document(bundle.lifecycle_bytes, _MAX_LIFECYCLE_BYTES, "lifecycle")
        )
    except ValueError as error:
        raise Task1LegacyArtifactError("Task 1 lifecycle receipt is invalid.") from error
    match lifecycle.phase:
        case "baseline_epoch":
            if lifecycle.evidence_sha256 != sha256(bundle.host_a_preflight_bytes).hexdigest():
                raise Task1LegacyArtifactError(
                    "Task 1 baseline lifecycle is not bound to Host A preflight."
                )
        case "final_epoch":
            pass
        case "host_a_epoch" | "teardown_verified":
            raise Task1LegacyArtifactError("Task 1 lifecycle phase is not importable.")
        case unexpected:
            assert_never(unexpected)
    if (
        lifecycle.machine_sha256 != preflight["machine_sha256"]
        or lifecycle.ssh_sha256 != preflight["ssh_sha256"]
        or lifecycle.architecture != preflight["architecture"]
        or lifecycle.baseline_boot_sha256 != preflight["boot_sha256"]
    ):
        raise Task1LegacyArtifactError("Task 1 lifecycle identity drifted from preflight.")
    return ParsedTask1LegacyArtifacts(ledger, lifecycle, state_files)


def proof_document(content: bytes, limit: int, label: str) -> dict[str, proof.JsonValue]:
    """Parse one bounded canonical trailing-newline JSON object."""

    import json

    if not content or len(content) > limit or not content.endswith(b"\n"):
        raise Task1LegacyArtifactError(
            f"Task 1 {label} bytes are not bounded canonical JSON."
        )
    try:
        value: proof.JsonValue = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Task1LegacyArtifactError(f"Task 1 {label} JSON is invalid.") from error
    if not isinstance(value, dict) or content != proof.canonical_json_bytes(value) + b"\n":
        raise Task1LegacyArtifactError(f"Task 1 {label} bytes are not canonical.")
    return value


def _parse_preflight(value: dict[str, proof.JsonValue]) -> dict[str, str]:
    if set(value) != _PREFLIGHT_KEYS or value.get("schema_version") != 1:
        raise Task1LegacyArtifactError("Task 1 Host A preflight schema is invalid.")
    if (
        value["namespace_clean"] is not True
        or value["host_identity_mode"] != "single_sequential"
        or value["host_identities_distinct"] is not False
        or value["provenance_role"] != "host_a"
        or value["temporal_clean_epoch_evidence"] is not False
    ):
        raise Task1LegacyArtifactError("Task 1 Host A preflight is not a clean target.")
    inventory = _mapping(value["inventory"], "Host A inventory")
    if set(inventory) != {"cloudflare", "coder", "docker", "dokploy", "tailscale"}:
        raise Task1LegacyArtifactError("Task 1 Host A inventory planes are invalid.")
    for plane, raw in inventory.items():
        payload = _mapping(raw, f"{plane} inventory")
        keys = {"resources", "state"}
        if plane == "docker" and "provenance" in payload:
            keys.add("provenance")
        if set(payload) != keys or payload["state"] not in {"absent", "present"}:
            raise Task1LegacyArtifactError("Task 1 Host A inventory state is invalid.")
        resources = payload["resources"]
        if not isinstance(resources, list) or (payload["state"] == "absent") != (not resources):
            raise Task1LegacyArtifactError("Task 1 Host A inventory absence is invalid.")
        resource_keys = (
            {"fingerprint_sha256", "id", "kind", "match", "provenance"}
            if plane == "cloudflare"
            else {"id", "kind", "name"}
        )
        if not all(
            isinstance(item, dict)
            and set(item) == resource_keys
            and all(isinstance(field, str) and field for field in item.values())
            for item in resources
        ):
            raise Task1LegacyArtifactError("Task 1 Host A resources are not typed.")
        if plane == "cloudflare" and any(item["match"] != "foreign" for item in resources):
            raise Task1LegacyArtifactError("Task 1 Host A contains target Cloudflare resources.")
    return {
        "architecture": _text(value["architecture"], "preflight architecture"),
        "boot_sha256": _hash(value["boot_sha256"], "preflight boot"),
        "machine_sha256": _hash(value["machine_sha256"], "preflight machine"),
        "ssh_sha256": _hash(value["ssh_sha256"], "preflight SSH"),
    }


def _parse_ledger(content: bytes) -> OwnershipLedger:
    payload = proof_document(content, _MAX_LEDGER_BYTES, "ownership ledger")
    if set(payload) != {"format_version", "resources"}:
        raise Task1LegacyArtifactError("Task 1 ownership ledger schema is invalid.")
    try:
        ledger = OwnershipLedger.from_dict(payload)
    except StateValidationError as error:
        raise Task1LegacyArtifactError("Task 1 ownership ledger is invalid.") from error
    if content != canonical_bytes(ledger.to_dict()):
        raise Task1LegacyArtifactError("Task 1 ownership ledger bytes are not canonical.")
    return ledger


def _parse_baseline(
    value: dict[str, proof.JsonValue], ledger_bytes: bytes
) -> tuple[str, ...]:
    if set(value) != _BASELINE_KEYS:
        raise Task1LegacyArtifactError("Task 1 baseline schema is invalid.")
    inventories = _mapping(value["resource_inventories"], "baseline resource inventories")
    if set(inventories) != {"tailscale", "wizard_state"}:
        raise Task1LegacyArtifactError("Task 1 baseline resource inventories are invalid.")
    state = _mapping(inventories["wizard_state"], "baseline wizard state")
    if set(state) != {"ledger_sha256", "resources", "state_sha256"}:
        raise Task1LegacyArtifactError("Task 1 baseline wizard-state schema is invalid.")
    if _hash(state["ledger_sha256"], "baseline ledger") != sha256(ledger_bytes).hexdigest():
        raise Task1LegacyArtifactError("Task 1 baseline ledger hash drifted.")
    _hash(state["state_sha256"], "baseline state")
    return _parse_state_files(state["resources"])


def _parse_state_files(value: proof.JsonValue) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise Task1LegacyArtifactError("Task 1 baseline state-file inventory is invalid.")
    files: list[str] = []
    for item in value:
        if not isinstance(item, str) or _SAFE_STATE_FILE.fullmatch(item) is None:
            raise Task1LegacyArtifactError("Task 1 baseline state-file inventory is invalid.")
        files.append(item)
    if (
        not files
        or files != sorted(files)
        or len(set(files)) != len(files)
        or "ownership-ledger.json" not in files
    ):
        raise Task1LegacyArtifactError("Task 1 baseline state-file inventory is invalid.")
    return tuple(files)


def _mapping(value: proof.JsonValue, label: str) -> dict[str, proof.JsonValue]:
    if not isinstance(value, dict):
        raise Task1LegacyArtifactError(f"Task 1 {label} must be an object.")
    return value


def _text(value: proof.JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Task1LegacyArtifactError(f"Task 1 {label} is invalid.")
    return value


def _hash(value: proof.JsonValue, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or set(text) - set("0123456789abcdef") or text == "0" * 64:
        raise Task1LegacyArtifactError(f"Task 1 {label} hash is invalid.")
    return text
