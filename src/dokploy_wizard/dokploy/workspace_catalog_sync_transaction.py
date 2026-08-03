from __future__ import annotations

from pathlib import Path

from dokploy_wizard.dokploy import workspace_catalog_sync_transition as _transitions
from dokploy_wizard.dokploy.workspace_catalog_sync_adoption import adopt_legacy as _adopt_legacy
from dokploy_wizard.dokploy.workspace_catalog_sync_cleanup import (
    advance_file_targets,
    cleanup_sensitive_files,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_finalize import (
    commit_transaction,
    rollback_transaction,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_io import (
    block_record,
    create_record,
    ensure_private_directory,
    persist_record,
    read_owned_record,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    CatalogTarget,
    LegacyAdoptionReceipt,
    LegacyAdoptionRequest,
    ProcessIdentity,
    TransactionBlockedError,
    TransactionCasError,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_path import (
    ensure_authorized_directory,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import ProcessControl
from dokploy_wizard.dokploy.workspace_catalog_sync_target_authorization import (
    authorize_target,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_targets import stage_target
from dokploy_wizard.dokploy.workspace_catalog_sync_transaction_lifecycle import (
    WorkspaceCatalogTransactionLifecycle,
)


class WorkspaceCatalogTransaction(WorkspaceCatalogTransactionLifecycle):
    def __init__(self, *, workspace_root: Path, generation: int) -> None:
        if generation < 1:
            raise WorkspaceCatalogSyncError("workspace transaction generation is invalid")
        self._workspace_root = workspace_root.resolve(strict=True)
        self._generation = generation
        self._root = self._workspace_root / ".local/state/dokploy-wizard/model-sync/transactions"
        self._transaction_dir = self._root / str(generation)
        self._record_path = self._transaction_dir / "transaction.json"
        for directory in (
            self._root,
            self._transaction_dir,
            self._transaction_dir / "preimages",
            self._transaction_dir / "staged",
        ):
            ensure_private_directory(directory, trusted_root=self._workspace_root)

    @property
    def transaction_dir(self) -> Path:
        return self._transaction_dir

    def prepare(
        self,
        *,
        targets: tuple[CatalogTarget, ...],
        processes: tuple[ProcessIdentity, ...] = (),
        explicit_operator_update: bool = False,
    ) -> TransactionRecord:
        if not targets:
            raise WorkspaceCatalogSyncError("workspace transaction targets are empty")
        normalized = tuple(authorize_target(self._workspace_root, target) for target in targets)
        if len({target.path for target in normalized}) != len(normalized):
            raise WorkspaceCatalogSyncError("workspace transaction targets are not unique")
        for target in normalized:
            ensure_authorized_directory(
                target.path.parent,
                trusted_root=self._workspace_root,
                private=False,
            )
        durable = False
        try:
            receipts = tuple(
                stage_target(
                    target,
                    preimage_path=self._artifact_path(index, "preimages"),
                    staged_path=self._artifact_path(index, "staged"),
                    explicit_operator_update=explicit_operator_update,
                    trusted_root=self._workspace_root,
                )
                for index, target in enumerate(normalized)
            )
            now = _transitions.timestamp()
            record = TransactionRecord(
                generation=self._generation,
                cas_token=_transitions.token(),
                phase="prepared",
                created_at=now,
                updated_at=now,
                catalog_sha256=_transitions.catalog_sha(normalized),
                targets=receipts,
                processes=_transitions.validate_processes(processes, self._generation),
                error=None,
            )
            create_record(
                self._record_path, record, trusted_root=self._workspace_root
            )
            durable = True
            return record
        finally:
            if not durable:
                cleanup_sensitive_files(
                    self._transaction_dir,
                    target_count=len(normalized),
                    trusted_root=self._workspace_root,
                )

    def current(self) -> TransactionRecord:
        return read_owned_record(self._record_path, self._workspace_root, self._generation)

    def commit(self, *, cas_token: str, crash_after: str | None = None) -> TransactionRecord:
        record = self._require_cas(cas_token)
        if record.phase != "prepared":
            raise WorkspaceCatalogSyncError("workspace transaction is not prepared")
        try:
            _transitions.crash_at(crash_after, "prepared")
            return self._advance_files(record, crash_after)
        except TransactionBlockedError as error:
            block_record(
                self._record_path,
                record,
                str(error),
                trusted_root=self._workspace_root,
            )
            raise

    def adopt_legacy(self, request: LegacyAdoptionRequest) -> LegacyAdoptionReceipt:
        return _adopt_legacy(self._workspace_root, self._transaction_dir, request)

    def _advance_files(
        self, record: TransactionRecord, crash: str | None = None
    ) -> TransactionRecord:
        record = advance_file_targets(
            record,
            persist=self._persist,
            staged_path=self._staged_path,
            crash_after=crash,
            trusted_root=self._workspace_root,
        )
        if record.processes:
            return record
        return self._commit(record)

    def _rollback(
        self, record: TransactionRecord, control: ProcessControl | None = None
    ) -> TransactionRecord:
        return rollback_transaction(
            record,
            control=control,
            transaction_dir=self._transaction_dir,
            trusted_root=self._workspace_root,
            persist=self._persist,
            preimage_path=lambda index: self._artifact_path(index, "preimages"),
            staged_path=self._staged_path,
        )

    def _commit(self, record: TransactionRecord) -> TransactionRecord:
        return commit_transaction(
            record,
            transaction_dir=self._transaction_dir,
            trusted_root=self._workspace_root,
            persist=self._persist,
        )

    def _persist(
        self, expected: TransactionRecord, record: TransactionRecord
    ) -> TransactionRecord:
        return persist_record(
            self._record_path,
            expected,
            record,
            trusted_root=self._workspace_root,
        )

    def _block(
        self, record: TransactionRecord, error: WorkspaceCatalogSyncError
    ) -> None:
        block_record(
            self._record_path,
            record,
            str(error),
            trusted_root=self._workspace_root,
        )

    def _require_cas(self, token: str) -> TransactionRecord:
        record = self.current()
        if token != record.cas_token:
            raise TransactionCasError("workspace transaction CAS token does not match")
        return record

    def _artifact_path(self, index: int, directory: str) -> Path:
        return self._transaction_dir / directory / f"{index}.bin"

    def _staged_path(self, index: int) -> Path:
        return self._artifact_path(index, "staged")
