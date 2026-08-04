from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeGuard, get_args

from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
)

SyncFailureCategory = Literal[
    "catalog_source",
    "catalog_state",
    "model_admin_conflict",
    "model_admin_http_400",
    "model_admin_http_401",
    "model_admin_http_403",
    "model_admin_http_404",
    "model_admin_http_409",
    "model_admin_http_422",
    "model_admin_http_500",
    "model_admin_http_502",
    "model_admin_http_503",
    "model_admin_http_other",
    "model_admin_inventory_blocked",
    "model_admin_inventory_data",
    "model_admin_inventory_deployment",
    "model_admin_inventory_masked",
    "model_admin_inventory_model_id",
    "model_admin_inventory_model_info",
    "model_admin_inventory_model_name",
    "model_admin_inventory_routing",
    "model_admin_inventory_shape",
    "model_admin_lost_create_absent",
    "model_admin_lost_create_mismatch",
    "model_admin_lost_create_multiple",
    "model_admin_lost_patch_absent",
    "model_admin_lost_patch_mismatch",
    "model_admin_lost_patch_multiple",
    "model_admin_transport",
    "model_admin_unknown",
    "model_admin_unowned_alias",
    "model_admin_write",
    "persistence",
    "runtime_config",
    "runtime_lock",
    "runtime_unknown",
]

SYNC_FAILURE_CATEGORIES: Final[frozenset[str]] = frozenset(
    category
    for category in get_args(SyncFailureCategory)
    if isinstance(category, str)
)

_HTTP_FAILURES: dict[str, SyncFailureCategory] = {
    "400": "model_admin_http_400",
    "401": "model_admin_http_401",
    "403": "model_admin_http_403",
    "404": "model_admin_http_404",
    "409": "model_admin_http_409",
    "422": "model_admin_http_422",
    "500": "model_admin_http_500",
    "502": "model_admin_http_502",
    "503": "model_admin_http_503",
}


@dataclass(frozen=True, slots=True)
class SyncRuntimeError(RuntimeError):
    category: SyncFailureCategory


def model_admin_failure(error: LiteLLMModelAdminError) -> SyncFailureCategory:
    reason = error.reason
    if reason.startswith("unowned alias "):
        return "model_admin_unowned_alias"
    lost_write_markers: tuple[tuple[str, SyncFailureCategory], ...] = (
        (
            "lost create response left no owned alias",
            "model_admin_lost_create_absent",
        ),
        ("lost create response mismatch", "model_admin_lost_create_mismatch"),
        (
            "lost create response left multiple owned aliases",
            "model_admin_lost_create_multiple",
        ),
        (
            "lost patch response left no owned alias",
            "model_admin_lost_patch_absent",
        ),
        ("lost patch response mismatch", "model_admin_lost_patch_mismatch"),
        (
            "lost patch response left multiple owned aliases",
            "model_admin_lost_patch_multiple",
        ),
    )
    for marker, category in lost_write_markers:
        if reason.startswith(marker):
            return category
    markers: tuple[tuple[str, SyncFailureCategory], ...] = (
        ("masked routing parameter", "model_admin_inventory_masked"),
        ("routing parameters must contain", "model_admin_inventory_routing"),
        ("deployment blocked must be nested", "model_admin_inventory_blocked"),
        ("inventory requires a data array", "model_admin_inventory_data"),
        ("inventory deployment", "model_admin_inventory_deployment"),
        ("inventory model_name", "model_admin_inventory_model_name"),
        ("inventory model_info.id", "model_admin_inventory_model_id"),
        ("inventory model_info", "model_admin_inventory_model_info"),
    )
    for marker, category in markers:
        if marker in reason:
            return category
    if "inventory" in reason:
        return "model_admin_inventory_shape"
    status_prefix = "LiteLLM model admin request failed with status "
    if reason.startswith(status_prefix):
        return _HTTP_FAILURES.get(
            reason.removeprefix(status_prefix),
            "model_admin_http_other",
        )
    if "transport failed" in reason:
        return "model_admin_transport"
    if isinstance(error, LiteLLMModelAdminWriteAmbiguity):
        return "model_admin_write"
    if isinstance(error, LiteLLMModelAdminConflict):
        return "model_admin_conflict"
    return "model_admin_unknown"


def is_sync_failure_category(value: str) -> TypeGuard[SyncFailureCategory]:
    return value in SYNC_FAILURE_CATEGORIES
