"""Host-pair identity and later-proof receipt contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

_SUPPORTED_ARCHITECTURES: Final = frozenset({"amd64", "arm64"})


@dataclass(frozen=True, slots=True)
class HostIdentity:
    machine_sha256: str
    ssh_sha256: str
    architecture: str


def assert_namespace_identity(*, host_a: HostIdentity, host_b: HostIdentity) -> None:
    """Require two physical hosts with one supported architecture before any upload."""
    if host_a.machine_sha256 == host_b.machine_sha256:
        raise ValueError("Host A and Host B must have distinct machine identities")
    if host_a.ssh_sha256 == host_b.ssh_sha256:
        raise ValueError("Host A and Host B must have distinct SSH host identities")
    if host_a.architecture not in _SUPPORTED_ARCHITECTURES:
        raise ValueError("Host A architecture is unsupported")
    if host_b.architecture not in _SUPPORTED_ARCHITECTURES:
        raise ValueError("Host B architecture is unsupported")
    if host_a.architecture != host_b.architecture:
        raise ValueError("Host A and Host B architectures must match")


def assert_followup_proof_contract(
    *,
    contract_name: Literal[
        "upgrade_host_a_contract", "final_proof_contract", "reseed_pair_contract"
    ],
    receipts: tuple[str, ...],
) -> None:
    """Keep later Host A/Host B actions blocked until their named receipt exists."""
    required = {
        "upgrade_host_a_contract": "host-a-baseline",
        "final_proof_contract": "host-a-destroyed",
        "reseed_pair_contract": "host-b-clean",
    }[contract_name]
    if required not in receipts:
        raise ValueError(f"{contract_name} requires receipt {required}")
