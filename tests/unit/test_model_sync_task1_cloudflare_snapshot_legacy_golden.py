from __future__ import annotations

import hashlib

from dokploy_wizard.proof import (
    canonical_json_bytes,
    derive_result_from_attestation,
    result_bytes_from_attestation,
)
from tests.unit._model_sync_task1_cloudflare_snapshot_attestation_support import (
    legacy_attestation,
)


def test_legacy_v2_attestation_and_result_bytes_remain_golden() -> None:
    # Given
    attestation = legacy_attestation()

    # When
    attestation_payload = attestation.to_payload()
    result = derive_result_from_attestation(attestation)
    attestation_digest = hashlib.sha256(canonical_json_bytes(attestation_payload)).hexdigest()
    result_digest = hashlib.sha256(result_bytes_from_attestation(attestation)).hexdigest()

    # Then
    assert "cloudflare_snapshot_evidence" not in attestation_payload
    assert "task1_cloudflare_snapshot_evidence" not in result
    assert (attestation_digest, result_digest) == (
        "fe09d11b21f184e14d470946b7b7f6c59988b1292e6efa7300cf0af2a50f3765",
        "93e5aa20929cc78efbcb0e7082fad4f4460b30eb34ee80f8ab7e3637350b75a4",
    )
