from __future__ import annotations

import argparse
from pathlib import Path

from dokploy_wizard.dokploy.coder_secret_workspace_authorization import (
    WorkspaceSupersessionContext,
    capture_workspace_supersession_authorization,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--machine-sha256", required=True)
    parser.add_argument("--ssh-sha256", required=True)
    parser.add_argument("--lifecycle-sha256", required=True)
    parser.add_argument("--stack-sha256", required=True)
    parser.add_argument("--final-commit", required=True)
    parser.add_argument("--attempt-context-sha256", required=True)
    args = parser.parse_args()
    capture_workspace_supersession_authorization(
        args.state_dir,
        args.output,
        WorkspaceSupersessionContext(
            machine_sha256=args.machine_sha256,
            ssh_sha256=args.ssh_sha256,
            lifecycle_sha256=args.lifecycle_sha256,
            stack_sha256=args.stack_sha256,
            final_commit=args.final_commit,
            attempt_context_sha256=args.attempt_context_sha256,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
