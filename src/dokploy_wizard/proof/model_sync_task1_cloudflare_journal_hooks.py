"""Test-only crash-boundary callbacks for Task 1 Cloudflare journal writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    Task1CloudflareJournalOperation,
)


@dataclass(frozen=True, slots=True)
class Task1CloudflareJournalHooks:
    """Deterministic crash boundaries; production leaves every callback unset."""

    after_intent_fsync: Callable[[Task1CloudflareJournalOperation], None] | None = None
    after_provider_response: Callable[[Task1CloudflareJournalOperation], None] | None = None
    before_created_checkpoint: Callable[[Task1CloudflareJournalOperation], None] | None = None
    after_cleanup_checkpoint: Callable[[Task1CloudflareJournalOperation], None] | None = None


def call_hook(
    hook: Callable[[Task1CloudflareJournalOperation], None] | None,
    operation: Task1CloudflareJournalOperation,
) -> None:
    """Run an injected test boundary without adding production behavior."""
    if hook is not None:
        hook(operation)
