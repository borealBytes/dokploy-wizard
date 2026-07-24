from __future__ import annotations

import hashlib
import json

import pytest

from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema import (
    CloudflareSnapshotCollectionV1,
    CloudflareSnapshotError,
    CloudflareSnapshotResourceV1,
    CloudflareSnapshotV1,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_schema_types import (
    KINDS,
    OTP_NAME_SHA256,
)


def test_snapshot_round_trip_when_resources_are_redacted_and_canonical() -> None:
    # Given: complete redacted resources in a noncanonical order.
    tunnel = CloudflareSnapshotResourceV1(
        kind="tunnel",
        identity_sha256="b" * 64,
        payload={
            "configuration_sha256": "c" * 64,
            "configuration_availability": "read",
            "config_src": "cloudflare",
            "name_sha256": "d" * 64,
            "spec_sha256": "e" * 64,
            "status": "healthy",
        },
    )
    provider = CloudflareSnapshotResourceV1(
        kind="identity_provider",
        identity_sha256="a" * 64,
        payload={
            "name_sha256": OTP_NAME_SHA256,
            "provider_type": "onetimepin",
            "spec_sha256": "f" * 64,
        },
    )
    collections = tuple(
        CloudflareSnapshotCollectionV1(
            kind,
            0 if kind == "access_policy" else 1,
            100,
            1 if kind in {"tunnel", "identity_provider"} else 0,
        )
        for kind in KINDS
    )

    # When: the canonical schema is published and reparsed.
    snapshot = CloudflareSnapshotV1.create(
        context_sha256="1" * 64,
        account_id_sha256="2" * 64,
        zone_id_sha256="3" * 64,
        collections=collections,
        resources=(tunnel, provider),
        otp_provider_sha256=provider.semantic_sha256,
    )
    content = snapshot.to_bytes()
    restored = CloudflareSnapshotV1.from_bytes(content)

    # Then: identity order and canonical bytes are stable without raw values.
    assert restored == snapshot
    assert [item.kind for item in restored.resources] == ["identity_provider", "tunnel"]
    assert content == restored.to_bytes()
    assert snapshot.snapshot_sha256 in content.decode()


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"provider_type": "onetimepin"}, "keys"),
        (
            {
                "name_sha256": "f" * 64,
                "provider_type": "",
                "spec_sha256": "e" * 64,
            },
            "text",
        ),
        (
            {
                "configuration_sha256": "g" * 64,
                "configuration_availability": "read",
                "config_src": "cloudflare",
                "name_sha256": "not-a-hash",
                "spec_sha256": "f" * 64,
                "status": "healthy",
            },
            "hash",
        ),
    ],
)
def test_snapshot_resource_rejects_unknown_or_malformed_fields_when_parsed(
    payload: dict[str, str], error: str
) -> None:
    # Given: one invalid resource projection.
    raw = {
        "kind": "identity_provider" if "provider_type" in payload else "tunnel",
        "identity_sha256": "a" * 64,
        "payload": payload,
    }

    # When / Then: schema parsing fails closed.
    with pytest.raises(CloudflareSnapshotError, match=error):
        CloudflareSnapshotResourceV1.from_payload(raw)


def test_snapshot_rejects_noncanonical_or_cross_bound_bytes_when_parsed() -> None:
    # Given: a valid canonical snapshot payload with a changed context binding.
    provider = CloudflareSnapshotResourceV1(
        kind="identity_provider",
        identity_sha256="a" * 64,
        payload={
            "name_sha256": OTP_NAME_SHA256,
            "provider_type": "onetimepin",
            "spec_sha256": "b" * 64,
        },
    )
    snapshot = CloudflareSnapshotV1.create(
        context_sha256="1" * 64,
        account_id_sha256="2" * 64,
        zone_id_sha256="3" * 64,
        collections=tuple(
            CloudflareSnapshotCollectionV1(
                kind,
                0 if kind == "access_policy" else 1,
                100,
                1 if kind == "identity_provider" else 0,
            )
            for kind in KINDS
        ),
        resources=(provider,),
        otp_provider_sha256=provider.semantic_sha256,
    )
    payload = json.loads(snapshot.to_bytes())
    payload["context_sha256"] = "4" * 64

    # When / Then: a recomputed-looking but unbound payload is rejected.
    with pytest.raises(CloudflareSnapshotError, match="snapshot hash"):
        CloudflareSnapshotV1.from_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )


def test_snapshot_rejects_rehashed_payload_when_empty_plane_is_omitted() -> None:
    # Given: a rehashed canonical-looking snapshot missing certificate metadata.
    provider = CloudflareSnapshotResourceV1(
        "identity_provider",
        "a" * 64,
        {"name_sha256": OTP_NAME_SHA256, "provider_type": "onetimepin", "spec_sha256": "b" * 64},
    )
    snapshot = CloudflareSnapshotV1.create(
        context_sha256="1" * 64,
        account_id_sha256="2" * 64,
        zone_id_sha256="3" * 64,
        collections=tuple(
            CloudflareSnapshotCollectionV1(
                kind,
                0 if kind == "access_policy" else 1,
                100,
                1 if kind == "identity_provider" else 0,
            )
            for kind in KINDS
        ),
        resources=(provider,),
        otp_provider_sha256=provider.semantic_sha256,
    )
    payload = json.loads(snapshot.to_bytes())
    payload["collections"] = [
        item for item in payload["collections"] if item["kind"] != "certificate_pack"
    ]
    core = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
    payload["snapshot_sha256"] = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    ).hexdigest()

    # When / Then: self-consistent bytes still fail closed on missing coverage.
    with pytest.raises(CloudflareSnapshotError, match="collections"):
        CloudflareSnapshotV1.from_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
