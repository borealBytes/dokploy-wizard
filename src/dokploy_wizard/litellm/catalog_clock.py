from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from dokploy_wizard.litellm.catalog_types import SourceContractError


class CatalogClock(Protocol):
    def now(self) -> datetime: ...


def read_catalog_clock(clock: CatalogClock) -> datetime:
    observed_at = clock.now()
    if (
        observed_at.tzinfo is None
        or observed_at.utcoffset() != timedelta(0)
        or observed_at.tzinfo is not UTC
    ):
        raise SourceContractError("catalog clock must return an aware UTC datetime")
    return observed_at
