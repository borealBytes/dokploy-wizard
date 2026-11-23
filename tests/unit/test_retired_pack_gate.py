"""Gate: the retired packs must stay out of ``src/``.

OpenClaw, Nexa, Telly, My Farm Advisor, and SurfSense were retired code-only: no
pack definition, no env metadata, and no lifecycle phase may reference them, so
this test greps every source file for case-insensitive traces of the retired
names and asserts there are none.

One file is deliberately exempt: ``src/dokploy_wizard/uninstall/planner.py``.
Uninstall is driven by the ownership ledger, and ledgers written by older wizard
versions still carry ``openclaw_service``/``my_farm-advisor``/``surfsense_service``
resource types with their deletion phases. The planner keeps those literal
insurance constants so an old box that ran the retired packs can still be
uninstalled cleanly (and its volumes destroyed); deleting the constants would
strand those resources on exactly the hosts where they exist.

Pattern note: the ``advisor`` term carries a trailing ``(?![a-z])`` guard so the
plain English words "advisory"/"advisories" (e.g. preflight's ``advisories``
field) do not trip the gate, while retired identifiers such as
``MY_FARM_ADVISOR_*`` or the ``my-farm-advisor`` phase literal still match.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"

_RETIRED_PACK_PATTERN = re.compile(
    r"openclaw|nexa|telly|my.?farm|surfsense|advisor(?![a-z])",
    re.IGNORECASE,
)

# Files allowed to mention retired packs; see the module docstring for why.
_EXEMPT_RELATIVE_PATHS = frozenset(
    {
        Path("dokploy_wizard") / "uninstall" / "planner.py",
    }
)

_PLANNER_INSURANCE_LITERALS = (
    "MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE",
    "OPENCLAW_MEM0_SERVICE_RESOURCE_TYPE",
    "OPENCLAW_QDRANT_SERVICE_RESOURCE_TYPE",
    "OPENCLAW_RUNTIME_SERVICE_RESOURCE_TYPE",
    "OPENCLAW_SERVICE_RESOURCE_TYPE",
    "SURFSENSE_DATA_RESOURCE_TYPE",
    "SURFSENSE_SERVICE_RESOURCE_TYPE",
)


def _scanned_source_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def test_src_has_no_retired_pack_references_outside_the_uninstall_planner() -> None:
    assert _SRC_ROOT.is_dir(), f"missing source tree at {_SRC_ROOT}"

    offenders: list[str] = []
    for path in _scanned_source_files():
        relative = path.relative_to(_SRC_ROOT)
        if relative in _EXEMPT_RELATIVE_PATHS:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _RETIRED_PACK_PATTERN.search(line):
                offenders.append(f"{relative.as_posix()}:{lineno}: {line.strip()}")

    assert offenders == [], "retired pack references remain in src/:\n" + "\n".join(offenders)


def test_uninstall_planner_still_carries_the_retired_pack_insurance_constants() -> None:
    # The exemption above is only valid while the planner keeps the literal
    # resource types that let old boxes uninstall their retired-pack services.
    planner_source = (_SRC_ROOT / "dokploy_wizard" / "uninstall" / "planner.py").read_text(
        encoding="utf-8"
    )

    for literal in _PLANNER_INSURANCE_LITERALS:
        assert literal in planner_source, (
            f"uninstall planner lost its retired-pack insurance constant {literal!r}; "
            "old boxes can no longer uninstall those services."
        )
