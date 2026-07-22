"""Canonical serialized preflight evidence."""

from __future__ import annotations

from typing import Literal, assert_never

from dokploy_wizard.proof import HostIdentityMode
from dokploy_wizard.proof.model_sync_artifacts import JsonValue
from dokploy_wizard.proof.model_sync_identity import RemoteProbe

PreflightRole = Literal["host_a", "host_b"]


def preflight_evidence(
    probe: RemoteProbe, *, mode: HostIdentityMode, role: PreflightRole
) -> dict[str, JsonValue]:
    """Bind an observed probe to its explicit host role."""
    match mode:
        case "distinct":
            identities_distinct = True
        case "single_sequential":
            if role != "host_a":
                raise ValueError("single-host preflight cannot claim Host B provenance")
            identities_distinct = False
        case unexpected:
            assert_never(unexpected)
    return {
        **probe.to_dict(),
        "host_identities_distinct": identities_distinct,
        "host_identity_mode": mode,
        "provenance_role": role,
        "schema_version": 1,
        "temporal_clean_epoch_evidence": False,
    }
