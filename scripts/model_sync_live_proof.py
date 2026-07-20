#!/usr/bin/env python3
"""Run the Coder/LiteLLM model-sync live proof without printing secret values."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

def run() -> int:
    from dokploy_wizard.proof.model_sync_cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
