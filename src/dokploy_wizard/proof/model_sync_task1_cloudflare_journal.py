"""Task 1 Cloudflare journal public API and guarded cleanup CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dokploy_wizard.proof.model_sync_task1_cloudflare_cleanup_report import (
    Task1CloudflareCleanupRun,
    run_task1_cloudflare_cleanup,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_hooks import (
    Task1CloudflareJournalHooks,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_runtime import (
    Task1CloudflareJournal,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalCleanupState,
    JournalOperationKind,
    JournalOperationState,
    JournalStatus,
    Task1CloudflareJournalDocument,
    Task1CloudflareJournalIntent,
    Task1CloudflareJournalOperation,
    Task1CloudflareJournalVersion,
)
from dokploy_wizard.proof.model_sync_task1_context import (
    activate_task1_proof_context,
    validate_task1_proof_context_argument,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError

__all__ = (
    "JournalCleanupState",
    "JournalOperationKind",
    "JournalOperationState",
    "JournalStatus",
    "Task1CloudflareJournal",
    "Task1CloudflareJournalDocument",
    "Task1CloudflareJournalHooks",
    "Task1CloudflareJournalIntent",
    "Task1CloudflareJournalOperation",
    "Task1CloudflareJournalVersion",
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model-sync-task1-cloudflare-cleanup")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--task1-proof-context", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from dokploy_wizard.networking.cloudflare import CloudflareApiBackend
        from dokploy_wizard.proof.model_sync_task1_cloudflare_intent_reconciliation import (
            reconcile_incomplete_intents,
        )
        from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_api import (
            CloudflareSnapshotApiBackend,
        )
        from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
            CloudflareSnapshotScope,
        )
        from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_collection import (
            capture_cloudflare_snapshot,
        )
        from dokploy_wizard.state import parse_env_file
        from dokploy_wizard.state.dokploy_runtime_auth import (
            load_dokploy_runtime_auth,
            merge_dokploy_runtime_auth,
        )

        raw_env = parse_env_file(args.env_file)
        context = validate_task1_proof_context_argument(raw_env, args.task1_proof_context)
        if context is None:
            raise Task1ProofContextError("Task 1 Cloudflare cleanup requires an active context")
        raw_env = merge_dokploy_runtime_auth(raw_env, load_dokploy_runtime_auth(args.state_dir))
        with activate_task1_proof_context(context):
            runtime = CloudflareApiBackend(raw_env)
            account_id = raw_env.values.get("CLOUDFLARE_ACCOUNT_ID", "")
            zone_id = raw_env.values.get("CLOUDFLARE_ZONE_ID") or runtime.resolve_zone_id(
                account_id, context.root_domain
            )
            if account_id == "" or zone_id is None:
                raise Task1ProofContextError("Task 1 Cloudflare snapshot scope is unavailable")
            scope = CloudflareSnapshotScope(
                hashlib.sha256(context.to_bytes()).hexdigest(), account_id, zone_id
            )
            snapshot_backend = CloudflareSnapshotApiBackend(raw_env)
            journal = Task1CloudflareJournal.for_context(state_dir=args.state_dir, context=context)
            receipt = run_task1_cloudflare_cleanup(
                Task1CloudflareCleanupRun(
                    state_dir=args.state_dir,
                    scope=scope,
                    journal=journal,
                    backend=runtime,
                    capture_snapshot=lambda: capture_cloudflare_snapshot(snapshot_backend, scope),
                    reconcile_intents=lambda: reconcile_incomplete_intents(
                        journal=journal,
                        backend=snapshot_backend,
                        scope=scope,
                    ),
                )
            )
    except (OSError, Task1ProofContextError, ValueError) as error:
        print(f"Task 1 Cloudflare cleanup failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
