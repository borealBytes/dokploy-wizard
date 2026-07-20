"""Task 1 result payload construction with the required non-secret keys."""

from __future__ import annotations

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


def build_result(values: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Reject incomplete result documents before their protected finalization."""
    if frozenset(values) != REQUIRED_RESULT_KEYS:
        raise ValueError("Task 1 result keys do not match the proof contract")
    return dict(values)
