"""Value-free remote state-upgrade authority observation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dokploy_wizard.proof.model_sync_task1_context import (
    Task1ProofContextError,
    activate_task1_proof_context,
    deactivate_task1_proof_context,
    validate_task1_proof_context_argument,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import PROOF_CONTROL_KEYS
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    RawEnvInput,
    StateValidationError,
    parse_env_file,
    resolve_desired_state,
)
from dokploy_wizard.state.store import parse_desired_state_payload
from dokploy_wizard.state.upgrade import state_upgrade_paths
from dokploy_wizard.state.upgrade_intent import ALL_KEYS, StateUpgradeIntent
from dokploy_wizard.state.upgrade_io import file_hash, read_json
from dokploy_wizard.state.upgrade_legacy_preimage import (
    legacy_receipt_without_runtime_images_fingerprint,
)


def observe_state_upgrade_authority(state_dir: Path) -> dict[str, bool | int | str]:
    """Read and summarize the exact authority required for upgrade recovery."""

    paths = state_upgrade_paths(state_dir)
    desired_present = paths["desired"].exists()
    applied_present = paths["applied"].exists()
    owner_path = state_dir / "shared-core-sync-owner.json"
    intent_path = state_dir / "state-upgrade-intent-v1.json"
    if not desired_present or not applied_present:
        return {
            "schema_version": 1,
            "desired_present": desired_present,
            "applied_present": applied_present,
            "intent_present": intent_path.exists(),
            "owner_present": owner_path.exists(),
        }
    desired_payload = read_json(paths["desired"])
    applied = AppliedStateCheckpoint.from_dict(read_json(paths["applied"]))
    desired = parse_desired_state_payload(desired_payload)
    raw_input_fingerprint: str | None = None
    receipt_raw_fingerprint: str | None = None
    legacy_receipt_fingerprint: str | None = None
    context_free_legacy_fingerprint: str | None = None
    raw_input: RawEnvInput | None = None
    raw_input_desired_reconstructable = False
    receipt_raw_desired_reconstructable = False
    raw_input_reconstruction_failure = "absent"
    if paths["raw_input"].exists():
        try:
            raw_input = RawEnvInput.from_dict(read_json(paths["raw_input"]))
        except StateValidationError:
            raw_input_reconstruction_failure = "state_validation"
        else:
            try:
                raw_input_fingerprint = resolve_desired_state(raw_input).fingerprint()
            except StateValidationError:
                raw_input_reconstruction_failure = "state_validation"
            except Task1ProofContextError:
                raw_input_reconstruction_failure = "task1_context"
            else:
                raw_input_desired_reconstructable = True
                raw_input_reconstruction_failure = "none"
            receipt_raw = RawEnvInput(
                format_version=raw_input.format_version,
                values={
                    key: value
                    for key, value in raw_input.values.items()
                    if key not in PROOF_CONTROL_KEYS
                },
            )
            try:
                receipt_raw_fingerprint = resolve_desired_state(receipt_raw).fingerprint()
            except (StateValidationError, Task1ProofContextError):
                receipt_raw_desired_reconstructable = False
            else:
                receipt_raw_desired_reconstructable = True
            legacy_receipt_fingerprint = legacy_receipt_without_runtime_images_fingerprint(
                raw_input,
                desired,
                applied,
            )
            with deactivate_task1_proof_context():
                context_free_legacy_fingerprint = (
                    legacy_receipt_without_runtime_images_fingerprint(
                        raw_input,
                        desired,
                        applied,
                    )
                )
    desired_fingerprint = hashlib.sha256(
        json.dumps(desired_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    desired_without_runtime_images = dict(desired_payload)
    desired_without_runtime_images.pop("runtime_images", None)
    desired_without_runtime_images_fingerprint = hashlib.sha256(
        json.dumps(
            desired_without_runtime_images,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    intent = _load_intent(intent_path)
    owner = _load_owner(owner_path)
    if intent is None or owner is None:
        return {
            "schema_version": 1,
            "desired_present": True,
            "applied_present": True,
            "desired_schema_valid": True,
            "applied_schema_valid": True,
            "applied_matches_current_desired": applied.desired_state_fingerprint
            == desired_fingerprint,
            "applied_matches_raw_input_desired": applied.desired_state_fingerprint
            == raw_input_fingerprint,
            "applied_matches_parsed_current_desired": applied.desired_state_fingerprint
            == desired.fingerprint(),
            "applied_matches_current_without_runtime_images": (
                applied.desired_state_fingerprint == desired_without_runtime_images_fingerprint
            ),
            "applied_matches_receipt_raw_desired": applied.desired_state_fingerprint
            == receipt_raw_fingerprint,
            "applied_matches_legacy_receipt_without_runtime_images": (
                applied.desired_state_fingerprint == legacy_receipt_fingerprint
            ),
            "context_free_legacy_receipt_candidate_available": (
                context_free_legacy_fingerprint is not None
            ),
            "applied_matches_context_free_legacy_receipt": (
                applied.desired_state_fingerprint == context_free_legacy_fingerprint
            ),
            "raw_input_desired_reconstructable": raw_input_desired_reconstructable,
            "raw_input_reconstruction_failure": raw_input_reconstruction_failure,
            "receipt_raw_desired_reconstructable": receipt_raw_desired_reconstructable,
            "intent_present": intent is not None,
            "owner_present": owner is not None,
        }
    expected_hashes = {
        key: intent.post_hashes[key] if key in intent.completed_writes else intent.pre_hashes[key]
        for key in ALL_KEYS
    }
    current_paths = {**paths, "owner": owner_path}
    return {
        "schema_version": 1,
        "desired_present": True,
        "applied_present": True,
        "desired_schema_valid": True,
        "applied_schema_valid": True,
        "applied_matches_current_desired": applied.desired_state_fingerprint
        == desired_fingerprint,
        "applied_matches_raw_input_desired": applied.desired_state_fingerprint
        == raw_input_fingerprint,
        "applied_matches_parsed_current_desired": applied.desired_state_fingerprint
        == desired.fingerprint(),
        "applied_matches_current_without_runtime_images": (
            applied.desired_state_fingerprint == desired_without_runtime_images_fingerprint
        ),
        "applied_matches_receipt_raw_desired": applied.desired_state_fingerprint
        == receipt_raw_fingerprint,
        "applied_matches_legacy_receipt_without_runtime_images": (
            applied.desired_state_fingerprint == legacy_receipt_fingerprint
        ),
        "context_free_legacy_receipt_candidate_available": (
            context_free_legacy_fingerprint is not None
        ),
        "applied_matches_context_free_legacy_receipt": (
            applied.desired_state_fingerprint == context_free_legacy_fingerprint
        ),
        "raw_input_desired_reconstructable": raw_input_desired_reconstructable,
        "raw_input_reconstruction_failure": raw_input_reconstruction_failure,
        "receipt_raw_desired_reconstructable": receipt_raw_desired_reconstructable,
        "intent_present": True,
        "intent_schema_valid": True,
        "owner_present": True,
        "owner_schema_valid": True,
        "intent_owner_matches": intent.owner_id == owner,
        "intent_status": intent.status,
        "intent_completed_write_count": len(intent.completed_writes),
        "intent_hash_map_matches_current_state": all(
            file_hash(current_paths[key]) == expected_hashes[key] for key in ALL_KEYS
        ),
        "intent_authorizes_desired_applied_mixed_state": (
            intent.status == "writing"
            and intent.completed_writes == ("owner", "desired")
            and applied.desired_state_fingerprint != desired_fingerprint
            and intent.pre_hashes["desired"] != intent.post_hashes["desired"]
            and intent.pre_hashes["applied"] == intent.post_hashes["applied"]
            and all(file_hash(current_paths[key]) == expected_hashes[key] for key in ALL_KEYS)
        ),
    }


def _load_intent(path: Path) -> StateUpgradeIntent | None:
    if not path.exists():
        return None
    return StateUpgradeIntent.from_dict(read_json(path))


def _load_owner(path: Path) -> str | None:
    if not path.exists():
        return None
    payload = read_json(path)
    if set(payload) != {"owner_id", "schema_version"}:
        raise ValueError("State upgrade owner document is invalid.")
    owner_id = payload["owner_id"]
    schema_version = payload["schema_version"]
    if not isinstance(owner_id, str) or type(schema_version) is not int:
        raise ValueError("State upgrade owner document is invalid.")
    return owner_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--task1-proof-context", type=Path, required=True)
    args = parser.parse_args()
    raw_env = parse_env_file(args.env_file)
    context = validate_task1_proof_context_argument(raw_env, args.task1_proof_context)
    with activate_task1_proof_context(context):
        observation = observe_state_upgrade_authority(args.state_dir)
    observation["task1_context_active"] = context is not None
    print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
