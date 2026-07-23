from __future__ import annotations

import hashlib

import pytest

from dokploy_wizard.proof.model_sync_artifacts import CaptureSchemaError, JsonValue
from dokploy_wizard.proof.model_sync_cloudflare import (
    classify_cloudflare_post_install,
    classify_cloudflare_preflight,
    cloudflare_evidence_payload,
    cloudflare_snapshot_sha256,
    preexisting_cloudflare_sha256,
)
from dokploy_wizard.proof.model_sync_cloudflare_probe import classify_post_install_resources
from dokploy_wizard.proof.model_sync_env import ProofNamespace
from dokploy_wizard.proof.model_sync_identity import ObservedResource


def _fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _record(
    *,
    resource_id: str = "resource-id",
    name: str = "coder.example.test",
    kind: str = "dns_record",
    fingerprint: str | None = None,
) -> dict[str, JsonValue]:
    return {
        "fingerprint_sha256": _fingerprint(resource_id) if fingerprint is None else fingerprint,
        "id": resource_id,
        "kind": kind,
        "name": name,
    }


def test_preexisting_exact_resource_is_redacted_and_preserve_only() -> None:
    # Given
    sensitive_name = "coder.example.test"
    resources = [_record(name=sensitive_name)]

    # When
    observed = classify_cloudflare_preflight(resources, {sensitive_name}, "proof-stack")

    # Then
    assert observed[0].to_dict() == {
        "fingerprint_sha256": _fingerprint("resource-id"),
        "id": "resource-id",
        "kind": "dns_record",
        "match": "exact",
        "provenance": "preexisting_unowned",
    }
    assert sensitive_name not in str(observed[0].to_dict())


@pytest.mark.parametrize(
    ("record", "error"),
    [
        (_record(fingerprint="A" * 64), "fingerprint"),
        (_record(kind="unsupported"), "unsupported"),
        (_record(name="proof-stack-unknown"), "prefix"),
        (_record(fingerprint=""), "fingerprint"),
    ],
)
def test_preflight_fails_closed_for_invalid_exact_or_prefix_resource(
    record: dict[str, JsonValue], error: str
) -> None:
    # Given
    names = {"coder.example.test"}

    # When / Then
    with pytest.raises(CaptureSchemaError, match=error):
        classify_cloudflare_preflight([record], names, "proof-stack")


def test_post_install_rejects_preexisting_drift_and_does_not_adopt_it() -> None:
    # Given
    names = {"coder.example.test"}
    before = classify_cloudflare_preflight([_record()], names, "proof-stack")
    drifted: list[JsonValue] = [_record(fingerprint=_fingerprint("mutated"))]

    # When / Then
    with pytest.raises(CaptureSchemaError, match="fingerprint"):
        classify_cloudflare_post_install(drifted, names, "proof-stack", before)


@pytest.mark.parametrize(
    "after",
    [
        [],
        [_record(resource_id="replacement-id")],
    ],
)
def test_post_install_rejects_preexisting_deletion_or_replacement(
    after: list[JsonValue],
) -> None:
    # Given
    names = {"coder.example.test"}
    before = classify_cloudflare_preflight([_record()], names, "proof-stack")

    # When / Then
    with pytest.raises(CaptureSchemaError, match="missing"):
        classify_cloudflare_post_install(after, names, "proof-stack", before)


@pytest.mark.parametrize(
    "records",
    [
        [_record(), _record()],
        [_record(), _record(resource_id="other-id")],
    ],
)
def test_preflight_rejects_duplicate_cloudflare_identity(
    records: list[JsonValue],
) -> None:
    # Given
    names = {"coder.example.test"}

    # When / Then
    with pytest.raises(CaptureSchemaError, match="duplicate"):
        classify_cloudflare_preflight(records, names, "proof-stack")


@pytest.mark.parametrize("kind", ["access_policy", "hostname_route"])
def test_preflight_allows_distinct_scoped_exact_resources_with_shared_name(kind: str) -> None:
    name = "coder.example.test"
    observed = classify_cloudflare_preflight(
        [
            _record(resource_id="first", name=name, kind=kind),
            _record(resource_id="second", name=name, kind=kind),
        ],
        {name},
        "proof-stack",
    )
    assert [item.provenance for item in observed] == ["preexisting_unowned", "preexisting_unowned"]


def test_post_install_keeps_preexisting_unowned_and_marks_only_new_exact_as_candidate() -> None:
    # Given
    names = {"coder.example.test", "tunnel-name"}
    before = classify_cloudflare_preflight([_record()], names, "proof-stack")

    # When
    observed = classify_cloudflare_post_install(
        [
            _record(),
            _record(resource_id="tunnel-id", name="tunnel-name", kind="tunnel"),
        ],
        names,
        "proof-stack",
        before,
    )

    # Then
    assert [item.provenance for item in observed] == [
        "preexisting_unowned",
        "created_candidate",
    ]
    assert preexisting_cloudflare_sha256(observed) == preexisting_cloudflare_sha256(before)


def test_complete_post_snapshot_is_stable_redacted_and_sensitive_to_new_records() -> None:
    # Given
    names = {"coder.example.test", "tunnel-name"}
    before = classify_cloudflare_preflight([_record()], names, "proof-stack")
    post = classify_cloudflare_post_install(
        [_record(), _record(resource_id="tunnel-id", name="tunnel-name", kind="tunnel")],
        names,
        "proof-stack",
        before,
    )

    # When
    payload = cloudflare_evidence_payload(post)
    reordered = cloudflare_snapshot_sha256(tuple(reversed(post)))

    # Then
    assert cloudflare_snapshot_sha256(post) == reordered
    assert cloudflare_snapshot_sha256(post) != cloudflare_snapshot_sha256(before)
    assert payload[1] == {
        "fingerprint_sha256": _fingerprint("tunnel-id"),
        "id": "tunnel-id",
        "kind": "tunnel",
        "match": "exact",
        "provenance": "created_candidate",
    }
    assert "tunnel-name" not in str(payload)


def test_adapter_classifies_transient_observed_resources_without_serializing_names() -> None:
    namespace = ProofNamespace("proof-stack", (), (), ("coder.example.test",), (), ())
    before = (
        ObservedResource(
            "dns-id",
            "coder.example.test",
            "dns_record",
            _fingerprint("before"),
            "exact",
            "preexisting_unowned",
        ),
    )
    after = before + (
        ObservedResource(
            "foreign-id",
            "foreign.example.test",
            "dns_record",
            _fingerprint("foreign"),
            "foreign",
            "foreign",
        ),
    )

    observed = classify_post_install_resources(before, after, namespace, ObservedResource)

    assert [item.provenance for item in observed] == ["preexisting_unowned", "foreign"]
    assert "coder.example.test" not in str(observed[0].to_dict())
