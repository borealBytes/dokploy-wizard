"""Bounded remote-proof wrapper command construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class BoundedProcessRunner(Protocol):
    """The bounded subprocess executor used by the proof wrapper."""

    def __call__(
        self,
        command: list[str],
        *,
        stdin: bytes,
        output_limit: int,
        timeout_seconds: int,
        label: str,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ProofWrapperInvocation:
    """Inputs needed to run one remote proof wrapper invocation."""

    wrapper: Path
    host: str
    password: str
    env_file: Path
    task1_proof_context: Path | None


def run_proof_wrapper(
    invocation: ProofWrapperInvocation,
    runner: BoundedProcessRunner,
) -> None:
    """Run the bounded remote proof wrapper using an optional Task 1 context."""
    command = [
        str(invocation.wrapper),
        "proof",
        "--host",
        invocation.host,
        "--password-stdin",
        "--env-file",
        str(invocation.env_file),
    ]
    if invocation.task1_proof_context is not None:
        command.extend(["--task1-proof-context", str(invocation.task1_proof_context)])
    runner(
        command,
        stdin=(invocation.password + "\n").encode(),
        output_limit=2 * 1024 * 1024,
        timeout_seconds=3600,
        label="remote proof wrapper",
    )
