"""Leaf parsers for raw Task 1 Cloudflare journal fields."""

from __future__ import annotations

from dokploy_wizard.proof.model_sync_task1_context_schema import Task1ProofContextError


def optional_error(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 256):
        raise Task1ProofContextError("Task 1 Cloudflare cleanup journal is invalid")
    return value
