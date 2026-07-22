"""Validated host input names for Task 1 proof execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import assert_never

from dokploy_wizard.proof import HostIdentityMode


@dataclass(frozen=True, slots=True)
class HostInputNames:
    host_a: str
    password_a: str
    host_b: str
    password_b: str


def resolve_host_inputs(names: HostInputNames, mode: HostIdentityMode) -> tuple[str, str, str, str]:
    """Read exactly the host credentials compatible with the chosen proof mode."""
    keys = (names.host_a, names.password_a, names.host_b, names.password_b)
    values = tuple(os.environ.get(key) for key in keys)
    if any(value is None or value == "" for value in values):
        missing = ", ".join(key for key, value in zip(keys, values, strict=True) if not value)
        raise RuntimeError(f"missing required external inputs: {missing}")
    host_a, password_a, host_b, password_b = values
    assert (
        host_a is not None
        and password_a is not None
        and host_b is not None
        and password_b is not None
    )
    match mode:
        case "distinct":
            if host_a == host_b:
                raise RuntimeError("same-host mapping requires --single-host-sequential")
        case "single_sequential":
            if (host_a, password_a) != (host_b, password_b):
                raise RuntimeError("single-host mode requires exact host and password mapping")
        case unexpected:
            assert_never(unexpected)
    return host_a, password_a, host_b, password_b
