from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dokploy_wizard.proof import model_sync_host_b
from dokploy_wizard.proof.model_sync_task1_context import (
    activate_task1_proof_context,
    validate_task1_proof_context_argument,
)
from dokploy_wizard.state import parse_env_file


def run_snapshot_cli(argv: Sequence[str] | None = None) -> int:
    """Run the bounded Host A snapshot command."""
    parser = argparse.ArgumentParser(prog="model-sync-snapshot")
    parser.add_argument("command", choices=("model-sync-snapshot",))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--task1-proof-context", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.task1_proof_context is None:
            snapshot = model_sync_host_b._snapshot(args.env_file, args.state_dir)
        else:
            context = validate_task1_proof_context_argument(
                parse_env_file(args.env_file), args.task1_proof_context
            )
            with activate_task1_proof_context(context):
                snapshot = model_sync_host_b._snapshot(args.env_file, args.state_dir)
        print(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"model-sync snapshot failed: {error}", file=sys.stderr)
        return 1
    return 0
