"""Typed Coder service teardown transaction contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CoderServiceTeardownError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CoderServiceTeardownBinding:
    stack_name: str
    resource_type: str
    resource_id: str
    resource_scope: str
    owner_id: str
