# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
EVIDENCE_DIR = REPO_ROOT / ".sisyphus" / "evidence"
TALK_SHARED_SECRET = "talk-shared-secret-test"
TALK_SIGNING_SECRET = "talk-signing-secret-test"
ONLYOFFICE_CALLBACK_SECRET = "onlyoffice-callback-secret-test"


def ts(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 19, hour, minute, second, tzinfo=UTC)


def load_json_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def build_talk_headers(body: bytes) -> dict[str, str]:
    signature = hmac.new(TALK_SIGNING_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Nextcloud-Talk-Secret": TALK_SHARED_SECRET,
        "X-Nextcloud-Talk-Signature": f"sha256={signature}",
    }


def build_onlyoffice_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Onlyoffice-Callback-Secret": ONLYOFFICE_CALLBACK_SECRET,
    }


def write_evidence(
    base_dir: Path,
    *,
    scenario: str,
    evidence: dict[str, Any],
) -> Path:
    del base_dir
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_path = EVIDENCE_DIR / f"{scenario}.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence_path
