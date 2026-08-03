"""Process lifecycle and blocked-state recovery for catalog transactions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path

from dokploy_wizard.dokploy import workspace_catalog_sync_transition as transitions
from dokploy_wizard.dokploy.workspace_catalog_sync_cleanup import (
    cleanup_sensitive_files,
    verify_resolution_targets,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    BlockedResolution,
    TransactionRecord,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process import ProcessControl
from dokploy_wizard.dokploy.workspace_catalog_sync_process_actions import (
    start_processes as _start_processes,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process_actions import (
    stop_processes as _stop_processes,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_process_actions import (
    verify_health as _verify_health,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_recovery import (
    RecoveryActions,
    recover_record,
)


class WorkspaceCatalogTransactionLifecycle(ABC):
    _workspace_root: Path
    _transaction_dir: Path

    def stop_processes(
        self, *, cas_token: str, control: ProcessControl, crash_after: str | None = None
    ) -> TransactionRecord:
        return _stop_processes(
            cas_token=cas_token,
            current=self._require_cas,
            persist=self._persist,
            block=self._block,
            control=control,
            crash_after=crash_after,
        )

    def start_processes(
        self, *, cas_token: str, control: ProcessControl, crash_after: str | None = None
    ) -> TransactionRecord:
        return _start_processes(
            cas_token=cas_token,
            current=self._require_cas,
            persist=self._persist,
            block=self._block,
            control=control,
            crash_after=crash_after,
        )

    def verify_health(
        self, *, cas_token: str, control: ProcessControl, crash_after: str | None = None
    ) -> TransactionRecord:
        return _verify_health(
            cas_token=cas_token,
            current=self._require_cas,
            persist=self._persist,
            block=self._block,
            commit=self._commit,
            control=control,
            crash_after=crash_after,
        )

    def recover(
        self,
        *,
        cas_token: str,
        control: ProcessControl | None = None,
        blocked_resolution: BlockedResolution | None = None,
    ) -> TransactionRecord:
        record = self._require_cas(cas_token)
        if record.phase in {"committed", "rolled_back"}:
            cleanup_sensitive_files(
                self._transaction_dir,
                target_count=len(record.targets),
                trusted_root=self._workspace_root,
            )
        return recover_record(
            record,
            control=control,
            blocked_resolution=blocked_resolution,
            actions=RecoveryActions(
                stop=self.stop_processes,
                start=self.start_processes,
                verify=self.verify_health,
                rollback=self._rollback,
                commit=self._commit,
                resolve=self.resolve_blocked,
            ),
        )

    def resolve_blocked(
        self,
        *,
        cas_token: str,
        resolution: BlockedResolution,
        control: ProcessControl | None = None,
    ) -> TransactionRecord:
        record = self._require_cas(cas_token)
        if record.phase != "blocked":
            raise WorkspaceCatalogSyncError("workspace transaction is not blocked")
        phase = transitions.inferred_blocked_phase(record)
        if resolution == "resume":
            verify_resolution_targets(
                record,
                accept_preimage=False,
                staged_path=self._staged_path,
                trusted_root=self._workspace_root,
            )
            reopened = self._persist(record, replace(record, phase=phase, error=None))
            if phase in {"prepared", "files_written", "pre_switch_verified", "switched"}:
                return self._advance_files(reopened)
            return self.recover(cas_token=reopened.cas_token, control=control)
        verify_resolution_targets(
            record,
            accept_preimage=True,
            staged_path=self._staged_path,
            trusted_root=self._workspace_root,
        )
        reopened = self._persist(record, replace(record, phase=phase, error=None))
        return self._rollback(reopened, control)

    @abstractmethod
    def _require_cas(self, token: str) -> TransactionRecord:
        ...

    @abstractmethod
    def _persist(
        self, expected: TransactionRecord, record: TransactionRecord
    ) -> TransactionRecord:
        ...

    @abstractmethod
    def _block(
        self, record: TransactionRecord, error: WorkspaceCatalogSyncError
    ) -> None:
        ...

    @abstractmethod
    def _advance_files(
        self, record: TransactionRecord, crash: str | None = None
    ) -> TransactionRecord:
        ...

    @abstractmethod
    def _rollback(
        self, record: TransactionRecord, control: ProcessControl | None = None
    ) -> TransactionRecord:
        ...

    @abstractmethod
    def _commit(self, record: TransactionRecord) -> TransactionRecord:
        ...

    @abstractmethod
    def _staged_path(self, index: int) -> Path:
        ...
