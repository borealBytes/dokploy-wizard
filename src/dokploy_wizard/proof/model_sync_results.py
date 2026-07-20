"""Task 1 result payload construction with the required non-secret keys."""

from __future__ import annotations

import re
from collections.abc import Mapping

from dokploy_wizard.proof.model_sync_artifacts import JsonValue

REQUIRED_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "source_base_commit",
        "proof_commit",
        "coder_image_digest",
        "litellm_image_digest",
        "shared_core_image_digests",
        "env_original_sha256",
        "env_proof_sha256",
        "env_mode",
        "external_backup_path",
        "abort_guard_path",
        "abort_guard_sha256",
        "host_a_preflight_sha256",
        "host_b_preflight_sha256",
        "host_identities_distinct",
        "host_architectures_equal",
        "baseline_sha256",
        "protected_artifacts_before_path",
        "protected_artifacts_before_sha256",
        "coder_secret_inventory_sha256",
        "legacy_workspace_managed_fingerprints_sha256",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def build_result(values: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Reject incomplete result documents before their protected finalization."""
    if frozenset(values) != REQUIRED_RESULT_KEYS:
        raise ValueError("Task 1 result keys do not match the proof contract")
    _require_hashes(values)
    _require_digests(values)
    _require_capture_values(values)
    return dict(values)


def _require_hashes(values: Mapping[str, JsonValue]) -> None:
    for key in (
        "env_original_sha256",
        "env_proof_sha256",
        "abort_guard_sha256",
        "host_a_preflight_sha256",
        "host_b_preflight_sha256",
        "baseline_sha256",
        "protected_artifacts_before_sha256",
        "coder_secret_inventory_sha256",
        "legacy_workspace_managed_fingerprints_sha256",
    ):
        value = values[key]
        if not isinstance(value, str) or not _SHA256.fullmatch(value) or value == "0" * 64:
            raise ValueError(f"{key} must be a non-zero SHA-256")


def _require_digests(values: Mapping[str, JsonValue]) -> None:
    image_values = [values["coder_image_digest"], values["litellm_image_digest"]]
    shared = values["shared_core_image_digests"]
    if not isinstance(shared, dict) or set(shared) != {"pgvector", "redis", "postfix", "litellm"}:
        raise ValueError("shared core image digest manifest is invalid")
    image_values.extend(shared.values())
    if any(not isinstance(value, str) or not _DIGEST.fullmatch(value) for value in image_values):
        raise ValueError("all captured images must use repository@sha256 digests")
    if shared["litellm"] != values["litellm_image_digest"]:
        raise ValueError("LiteLLM image observations must agree across result planes")


def _require_capture_values(values: Mapping[str, JsonValue]) -> None:
    commits = (values["source_base_commit"], values["proof_commit"])
    if any(not isinstance(value, str) or not _COMMIT.fullmatch(value) for value in commits):
        raise ValueError("proof commits must be exact SHA-1 values")
    if (
        values["schema_version"] != 1
        or not isinstance(values["env_mode"], int)
        or values["env_mode"] < 1
    ):
        raise ValueError("result schema version and proof env mode are invalid")
    if (
        values["host_identities_distinct"] is not True
        or values["host_architectures_equal"] is not True
    ):
        raise ValueError("result requires distinct hosts with matching architectures")
    for key in ("external_backup_path", "abort_guard_path", "protected_artifacts_before_path"):
        if not isinstance(values[key], str) or values[key] == "":
            raise ValueError(f"{key} must be a non-empty protected path")
