"""Module entrypoint for ``python -m dokploy_wizard``."""

from __future__ import annotations

import sys

from dokploy_wizard.cli import main

if __name__ == "__main__":
    sys.exit(main())
