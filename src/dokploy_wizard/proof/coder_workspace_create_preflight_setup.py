"""Classify Coder preflight setup without retaining setup values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from dokploy_wizard.proof.model_sync_env import EnvPreparationError, resolve_proof_transport
from dokploy_wizard.proof.model_sync_results import ProofTransport
from dokploy_wizard.proof.model_sync_task1_context import (
    Task1ProofContextError,
    Task1ProofContextV1,
    load_task1_proof_context,
)
from dokploy_wizard.state import StateValidationError, parse_env_file

SetupBlocker = Literal[
    "preflight_env_unavailable",
    "preflight_task1_context_unavailable",
    "preflight_transport_unavailable",
    "preflight_transport_configuration_invalid",
]


@dataclass(frozen=True, slots=True)
class PreflightSetupReady:
    """Typed setup dependencies allowed to reach the Coder read boundary."""

    context: Task1ProofContextV1
    transport: ProofTransport
    coder_hostname: str


@dataclass(frozen=True, slots=True)
class PreflightSetupBlocked:
    """Closed setup failure suitable for a value-free preflight report."""

    blocker: SetupBlocker


PreflightSetup: TypeAlias = PreflightSetupReady | PreflightSetupBlocked


def collect_preflight_setup(env_file: Path, context_file: Path) -> PreflightSetup:
    """Read preflight setup inputs and project known failure classes."""

    try:
        raw_env = parse_env_file(env_file)
    except (OSError, StateValidationError):
        return PreflightSetupBlocked("preflight_env_unavailable")
    try:
        context = load_task1_proof_context(context_file, raw_env)
    except Task1ProofContextError:
        return PreflightSetupBlocked("preflight_task1_context_unavailable")
    try:
        transport = resolve_proof_transport(env_file)
    except EnvPreparationError:
        return PreflightSetupBlocked("preflight_transport_unavailable")
    coder_hostname = transport.coder_hostname
    if coder_hostname is None:
        return PreflightSetupBlocked("preflight_transport_configuration_invalid")
    return PreflightSetupReady(context, transport, coder_hostname)
