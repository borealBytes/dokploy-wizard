from __future__ import annotations

import hashlib
import json

from dokploy_wizard.dokploy.coder_migration_types import JsonValue


def mutation_request_sha256(method: str, path: str, body: JsonValue) -> str:
    return _sha256({"body": body, "method": method, "path": path})


def mutation_response_sha256(
    method: str, path: str, body: JsonValue, response: JsonValue
) -> str:
    return _sha256({"body": body, "method": method, "path": path, "response": response})


def _sha256(value: JsonValue) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
