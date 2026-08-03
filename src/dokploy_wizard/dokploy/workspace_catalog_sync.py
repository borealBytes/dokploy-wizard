"""Shared workspace model-catalog refresh transaction and adapter surface."""

from __future__ import annotations

from dokploy_wizard.dokploy.workspace_catalog_sync_adapters import adapter_plan
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    AdapterPlan,
    CatalogModel,
    CatalogTarget,
    KdenseCatalogMetadata,
    LegacyAdoptionBinding,
    LegacyAdoptionReceipt,
    LegacyAdoptionRequest,
    LegacyPointerEvidence,
    ModelCatalog,
    OwnedPointer,
    ProcessIdentity,
    RuntimeIdentityError,
    TransactionBlockedError,
    TransactionCasError,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import (
    ProcessControl,
    observed_process_identity,
    signal_exact_process,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_transaction import WorkspaceCatalogTransaction

__all__ = [
    "AdapterPlan",
    "CatalogModel",
    "CatalogTarget",
    "KdenseCatalogMetadata",
    "LegacyAdoptionBinding",
    "LegacyAdoptionReceipt",
    "LegacyAdoptionRequest",
    "LegacyPointerEvidence",
    "ModelCatalog",
    "OwnedPointer",
    "ProcessIdentity",
    "ProcessControl",
    "RuntimeIdentityError",
    "TransactionBlockedError",
    "TransactionCasError",
    "TransactionRecord",
    "WorkspaceCatalogSyncError",
    "WorkspaceCatalogTransaction",
    "adapter_plan",
    "observed_process_identity",
    "signal_exact_process",
]
