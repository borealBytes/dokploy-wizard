"""Read-only remote artifact collection for the Host A upgrade proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.dokploy.coder_migration_receipt_types import MigrationReceipt
from dokploy_wizard.dokploy.coder_migration_receipt_validation import parse_receipt
from dokploy_wizard.dokploy.shared_core_schedule_receipt import schedule_record_payload
from dokploy_wizard.dokploy.sync_helper_docker import SubprocessDockerHelperRuntime
from dokploy_wizard.dokploy.sync_helper_lease import LeaseResult
from dokploy_wizard.litellm.catalog_persistence import STATE_FILENAME
from dokploy_wizard.litellm.catalog_persistence_fs import read_contract_file
from dokploy_wizard.litellm.catalog_state import parse_catalog_state
from dokploy_wizard.proof.model_sync_upgrade_host_a_observation_types import RETIRED_NAMES
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError
from dokploy_wizard.release_manifest import read_release_manifest
from dokploy_wizard.state import load_state_dir
from dokploy_wizard.state.sync_artifacts import SyncArtifactStore
from dokploy_wizard.state.sync_schema import JsonValue as SyncJsonValue
from dokploy_wizard.state.sync_schema import ScheduleSpec, canonical_digest
from dokploy_wizard.state.sync_state import AppliedSyncState, SyncDesiredState

_MODE_600: Final = 0o600
_MODE_644: Final = 0o644


def collect_host_a_observation(
    *,
    remote_root: Path,
    state_dir: Path,
    metadata_root: Path | None = None,
) -> dict[str, proof.JsonValue]:
    """Collect only typed, value-free receipt and runtime evidence."""

    loaded = load_state_dir(state_dir)
    if loaded.desired_state is None or loaded.applied_state is None:
        raise UpgradeHostAError("Host A desired or applied state is absent")
    desired_sync = loaded.desired_state.opencode_go_sync
    applied_sync = loaded.applied_state.opencode_go_sync
    resolved_metadata_root = metadata_root
    if desired_sync is not None and resolved_metadata_root is None:
        resolved_metadata_root = SubprocessDockerHelperRuntime().volume_mountpoint(
            desired_sync.metadata_volume
        )
    return {
        "catalog": _catalog(applied_sync, resolved_metadata_root),
        "migration": _migration(state_dir),
        "release": _release(remote_root),
        "schedule": _schedule(state_dir, desired_sync, applied_sync),
        "schema_version": 1,
        "sync_results": _sync_results(resolved_metadata_root),
    }


def _release(remote_root: Path) -> dict[str, proof.JsonValue]:
    manifest_path = remote_root / "release-manifest.json"
    _require_file(manifest_path, _MODE_644)
    manifest = read_release_manifest(manifest_path)
    active_path = (remote_root / "current").resolve(strict=True)
    expected_active = remote_root / "releases" / manifest.archive_sha256
    if active_path != expected_active:
        raise UpgradeHostAError("Host A active release does not match its manifest")
    return {
        "active_release_path": str(active_path),
        "archive_sha256": manifest.archive_sha256,
        "commit_sha": manifest.commit_sha,
        "manifest_mode": _MODE_644,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest.sha256,
    }


def _migration(state_dir: Path) -> dict[str, proof.JsonValue] | None:
    migration_dir = state_dir / "coder-template-migration"
    path = migration_dir / "coder-migration-receipts-v1.json"
    try:
        raw, _mode = proof.read_bounded_regular_bytes(path, 1024 * 1024, _MODE_600)
    except FileNotFoundError:
        return None
    receipt = parse_receipt(raw)
    events: list[str] = []
    for step in receipt.steps:
        request = step.request_sha256
        response = step.response_sha256
        if (request is None) != (response is None):
            raise UpgradeHostAError("Host A migration mutation receipt is incomplete")
        if request is not None and response is not None:
            if step.status != "verified":
                raise UpgradeHostAError("Host A migration mutation receipt is unverified")
            events.append(
                canonical_digest(
                    {
                        "kind": step.kind,
                        "request_sha256": request,
                        "response_sha256": response,
                        "step_id": step.step_id,
                    }
                )
            )
    primary_names = tuple(
        step.name
        for step in receipt.steps
        if step.kind == "rename_template" and step.name is not None
    )
    if len(primary_names) != 1:
        raise UpgradeHostAError("Host A migration primary receipt is absent or ambiguous")
    return {
        "mode": _MODE_600,
        "mutation_events": sorted(set(events)),
        "path": str(path),
        "primary_name": primary_names[0],
        "push_names": sorted(
            step.name
            for step in receipt.steps
            if step.kind == "push_template" and step.name is not None
        ),
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "retired_names": _retired_names(receipt),
        "status": receipt.status,
    }


def _retired_names(receipt: MigrationReceipt) -> list[str]:
    retired_steps = tuple(step for step in receipt.steps if step.kind == "delete_template")
    if len(retired_steps) != len(RETIRED_NAMES) or any(
        step.status != "verified" or step.request_sha256 is None or step.response_sha256 is None
        for step in retired_steps
    ):
        raise UpgradeHostAError("Host A retired template receipts are incomplete")
    return sorted(RETIRED_NAMES)


def _catalog(
    applied_sync: AppliedSyncState | None, metadata_root: Path | None
) -> dict[str, proof.JsonValue] | None:
    if applied_sync is None or metadata_root is None:
        return None
    model_set_sha256 = applied_sync.model_set_sha256
    if model_set_sha256 is None:
        return None
    state_path = metadata_root / STATE_FILENAME
    state_bytes = read_contract_file(state_path, "catalog state")
    if state_bytes is None:
        raise UpgradeHostAError("Host A catalog state is absent")
    state = parse_catalog_state(state_bytes)
    matches = tuple(
        path
        for path in (metadata_root / "generations").glob(f"*-{model_set_sha256}.json")
        if path.is_file()
    )
    if len(matches) != 1:
        raise UpgradeHostAError("Host A catalog generation is absent or ambiguous")
    generation = read_contract_file(matches[0], "catalog generation")
    if generation is None:
        raise UpgradeHostAError("Host A catalog generation is absent")
    _require_file(state_path, _MODE_600)
    _require_file(matches[0], _MODE_600)
    return {
        "applied_model_set_sha256": model_set_sha256,
        "generation_mode": _MODE_600,
        "generation_path": str(matches[0]),
        "generation_sha256": hashlib.sha256(generation).hexdigest(),
        "state_mode": _MODE_600,
        "state_output_sha256": state.last_output_sha256,
        "state_path": str(state_path),
    }


def _schedule(
    state_dir: Path,
    desired_sync: SyncDesiredState | None,
    applied_sync: AppliedSyncState | None,
) -> dict[str, proof.JsonValue] | None:
    if desired_sync is None or applied_sync is None:
        return None
    receipt_sha256 = applied_sync.last_sync_receipt_sha256
    if receipt_sha256 is None:
        return None
    receipt = SyncArtifactStore(state_dir).load_schedule_receipt(receipt_sha256)
    path = state_dir / "shared-core-sync" / "schedule-receipts" / f"{receipt_sha256}.json"
    _require_file(path, _MODE_600)
    remote = schedule_record_payload(receipt.remote)
    remote.pop("schedule_id")
    remote_spec = ScheduleSpec.from_dict(remote)
    return {
        "applied_desired_sha256": applied_sync.desired_fingerprint,
        "desired_sha256": desired_sync.fingerprint(),
        "desired_spec_sha256": desired_sync.schedule_spec_sha256,
        "receipt_mode": _MODE_600,
        "receipt_path": str(path),
        "receipt_sha256": receipt_sha256,
        "remote_spec_sha256": remote_spec.fingerprint,
    }


def _sync_results(metadata_root: Path | None) -> list[dict[str, proof.JsonValue]]:
    if metadata_root is None:
        return []
    result_dir = metadata_root / "lease-results"
    if not result_dir.exists():
        return []
    values: list[dict[str, proof.JsonValue]] = []
    for path in sorted(result_dir.glob("*.json")):
        raw = read_contract_file(path, "lease result")
        if raw is None:
            raise UpgradeHostAError("Host A synchronizer result disappeared")
        decoded: SyncJsonValue = json.loads(raw)
        if not isinstance(decoded, dict):
            raise UpgradeHostAError("Host A synchronizer result is malformed")
        result = LeaseResult.from_dict(decoded)
        _require_file(path, _MODE_600)
        values.append(
            {
                "durable_write_count": len(result.durable_write_delta),
                "mode": _MODE_600,
                "path": str(path),
                "result_sha256": result.sha256(),
            }
        )
    return sorted(values, key=lambda item: str(item["result_sha256"]))


def _require_file(path: Path, mode: int) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != mode:
        raise UpgradeHostAError("Host A observation artifact mode or type is invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    payload = collect_host_a_observation(
        remote_root=args.remote_root,
        state_dir=args.state_dir,
    )
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
