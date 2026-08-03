"""Crash-resumable outer-v1 augmentation for Shared Core sync state."""

from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, cast
from uuid import uuid4

from dokploy_wizard.state.models import OwnershipLedger
from dokploy_wizard.state.runtime_images import RuntimeImages
from dokploy_wizard.state.shared_core_sync import (
    AppliedSyncState,
    SyncDesiredState,
    SyncOwner,
    SyncStateError,
)
from dokploy_wizard.state.sync_schema import JsonValue
from dokploy_wizard.state.upgrade_intent import (
    BASE_KEYS,
    WRITE_ORDER,
    StateUpgradeError,
    StateUpgradeIntent,
)
from dokploy_wizard.state.upgrade_io import (
    atomic_bytes,
    atomic_json,
    file_hash,
    read_json,
)
from dokploy_wizard.state.upgrade_projection import (
    ledger_target_documents,
    runtime_target_documents,
    targets_match,
)
from dokploy_wizard.state.upgrade_projection import target_documents as build_target_documents

if TYPE_CHECKING:
    from dokploy_wizard.state.task1_legacy_authority_import import (
        Task1LegacyAuthorityImportRequest,
    )

__all__ = [
    "StateUpgradeError",
    "StateUpgradeIntent",
    "planned_sync_owner_id",
    "state_upgrade_paths",
    "upgrade_state_contract",
]


def upgrade_state_contract(
    *,
    state_dir: Path,
    owner_id: str,
    paths: Mapping[str, Path],
    sync_desired: SyncDesiredState | None = None,
    sync_applied: AppliedSyncState | None = None,
    runtime_images: RuntimeImages | None = None,
    ownership_ledger: OwnershipLedger | None = None,
    expected_generation: int | None = None,
    expected_cas_token: str | None = None,
    legacy_authority_import: Task1LegacyAuthorityImportRequest | None = None,
) -> StateUpgradeIntent:
    """Augment desired/applied state in owner-first order under a durable CAS intent."""

    if set(paths) != set(BASE_KEYS):
        raise StateUpgradeError("State upgrade paths must contain the exact seven base keys.")
    if sync_desired is not None and sync_desired.owner_id != owner_id:
        raise StateUpgradeError("State upgrade desired state belongs to a different owner.")
    if (sync_desired is None) != (sync_applied is None):
        raise StateUpgradeError("State upgrade sync desired/applied projection is incomplete.")
    if sync_desired is not None and runtime_images is not None:
        raise StateUpgradeError("State upgrade projection mode is ambiguous.")
    all_paths = {**paths, "owner": state_dir / "shared-core-sync-owner.json"}
    if sync_desired is not None and sync_applied is not None:
        target_documents = build_target_documents(
            paths,
            sync_desired,
            sync_applied,
            ownership_ledger,
        )
    elif runtime_images is not None and ownership_ledger is not None:
        target_documents = runtime_target_documents(paths, runtime_images, ownership_ledger)
    elif ownership_ledger is not None:
        target_documents = ledger_target_documents(paths, ownership_ledger)
    else:
        target_documents = None
    if legacy_authority_import is not None:
        from dokploy_wizard.state.task1_legacy_authority_import import (
            import_task1_legacy_authority,
        )

        import_task1_legacy_authority(state_dir, paths["ledger"], legacy_authority_import)
    state_dir.mkdir(parents=True, exist_ok=True)
    intent_path = state_dir / "state-upgrade-intent-v1.json"
    intent = _load_or_plan(
        intent_path,
        owner_id,
        all_paths,
        expected_generation,
        expected_cas_token,
    )
    _verify_resume(intent, all_paths)
    if intent.status == "complete":
        if target_documents is None or targets_match(target_documents, all_paths):
            return intent
        intent = _restart(intent, all_paths)
        atomic_json(intent_path, intent.to_dict())
    for key in WRITE_ORDER[len(intent.completed_writes) :]:
        if key == "owner":
            _write_owner(all_paths[key], owner_id)
        elif target_documents is None:
            atomic_bytes(all_paths[key], all_paths[key].read_bytes())
        else:
            atomic_json(all_paths[key], target_documents[key])
        post_hashes = {name: file_hash(path) for name, path in all_paths.items()}
        intent = _advance(intent, "writing", intent.completed_writes + (key,), post_hashes)
        atomic_json(intent_path, intent.to_dict())
    for key in ("raw_input", "litellm_keys", "surfsense_secrets", "seaweedfs_secrets"):
        if intent.pre_hashes[key] != intent.post_hashes[key]:
            raise StateUpgradeError("State upgrade changed protected raw or secret bytes.")
    complete = _advance(intent, "complete", WRITE_ORDER, intent.post_hashes)
    atomic_json(intent_path, complete.to_dict())
    return complete


def state_upgrade_paths(state_dir: Path) -> dict[str, Path]:
    return {
        "raw_input": state_dir / "raw-input.json",
        "desired": state_dir / "desired-state.json",
        "applied": state_dir / "applied-state.json",
        "ledger": state_dir / "ownership-ledger.json",
        "litellm_keys": state_dir / "litellm-generated-keys.json",
        "surfsense_secrets": state_dir / "surfsense-generated-secrets.json",
        "seaweedfs_secrets": state_dir / "seaweedfs-generated-secrets.json",
    }


def planned_sync_owner_id(state_dir: Path) -> str:
    path = state_dir / "shared-core-sync-owner.json"
    if not path.exists():
        return str(uuid4())
    try:
        return SyncOwner.from_dict(cast(dict[str, JsonValue], read_json(path))).owner_id
    except SyncStateError as error:
        raise StateUpgradeError("State upgrade owner document is malformed.") from error


def _load_or_plan(
    intent_path: Path,
    owner_id: str,
    paths: dict[str, Path],
    expected_generation: int | None,
    expected_cas_token: str | None,
) -> StateUpgradeIntent:
    if intent_path.exists():
        intent = StateUpgradeIntent.from_dict(read_json(intent_path))
        if intent.owner_id != owner_id:
            raise StateUpgradeError("State upgrade owner does not match the requested owner.")
        if expected_generation is not None and intent.generation != expected_generation:
            raise StateUpgradeError("State upgrade generation/token CAS mismatch.")
        if expected_cas_token is not None and intent.cas_token != expected_cas_token:
            raise StateUpgradeError("State upgrade generation/token CAS mismatch.")
        if intent.status == "complete":
            current_hashes = {name: file_hash(path) for name, path in paths.items()}
            if current_hashes != intent.post_hashes:
                if current_hashes["owner"] != intent.post_hashes["owner"]:
                    raise StateUpgradeError("State upgrade owner changed after completion.")
                now = datetime.now(tz=UTC).isoformat()
                intent = StateUpgradeIntent(
                    generation=intent.generation + 1,
                    cas_token=secrets.token_hex(32),
                    status="planned",
                    owner_id=owner_id,
                    pre_hashes=current_hashes,
                    post_hashes=current_hashes,
                    completed_writes=(),
                    created_at=now,
                    updated_at=now,
                )
                atomic_json(intent_path, intent.to_dict())
        return intent
    if expected_generation not in {None, 0} or expected_cas_token is not None:
        raise StateUpgradeError("State upgrade generation/token CAS mismatch.")
    now = datetime.now(tz=UTC).isoformat()
    hashes = {name: file_hash(path) for name, path in paths.items()}
    intent = StateUpgradeIntent(
        generation=1,
        cas_token=secrets.token_hex(32),
        status="planned",
        owner_id=owner_id,
        pre_hashes=hashes,
        post_hashes=hashes,
        completed_writes=(),
        created_at=now,
        updated_at=now,
    )
    atomic_json(intent_path, intent.to_dict())
    return intent


def _verify_resume(intent: StateUpgradeIntent, paths: dict[str, Path]) -> None:
    for key, path in paths.items():
        expected = (
            intent.post_hashes[key]
            if key in intent.completed_writes
            else intent.pre_hashes[key]
        )
        if file_hash(path) != expected:
            raise StateUpgradeError("State upgrade resume hash mismatch.")


def _advance(
    intent: StateUpgradeIntent,
    status: str,
    completed: tuple[str, ...],
    post_hashes: dict[str, str],
) -> StateUpgradeIntent:
    return replace(
        intent,
        generation=intent.generation + 1,
        cas_token=secrets.token_hex(32),
        status=status,
        completed_writes=completed,
        post_hashes=post_hashes,
        updated_at=datetime.now(tz=UTC).isoformat(),
    )


def _restart(
    previous: StateUpgradeIntent,
    paths: Mapping[str, Path],
) -> StateUpgradeIntent:
    now = datetime.now(tz=UTC).isoformat()
    hashes = {name: file_hash(path) for name, path in paths.items()}
    return StateUpgradeIntent(
        generation=previous.generation + 1,
        cas_token=secrets.token_hex(32),
        status="planned",
        owner_id=previous.owner_id,
        pre_hashes=hashes,
        post_hashes=hashes,
        completed_writes=(),
        created_at=now,
        updated_at=now,
    )


def _write_owner(path: Path, owner_id: str) -> None:
    if path.exists():
        try:
            owner = SyncOwner.from_dict(cast(dict[str, JsonValue], read_json(path)))
        except SyncStateError as error:
            raise StateUpgradeError("State upgrade owner document is malformed.") from error
        if owner.owner_id != owner_id:
            raise StateUpgradeError("State upgrade owner document does not match the intent.")
        return
    atomic_json(path, SyncOwner(owner_id=owner_id).to_dict())
