# pyright: reportMissingImports=false

from __future__ import annotations

import base64

import pytest

from dokploy_wizard.packs.openclaw.nexa_retrieval import (
    NexaCanonicalFileSnapshot,
    NexaFtsBootstrapState,
    NexaFtsCandidateHit,
    evaluate_fts_candidate_pointer,
    evaluate_retrieval_gate,
    resolve_retrieval_plan,
)
from dokploy_wizard.packs.openclaw.nexa_scope import NexaScopeContext


def test_resolve_retrieval_plan_keeps_webdav_canonical_even_with_fts_candidate_content() -> None:
    request_scope = NexaScopeContext(
        tenant_id="example.com",
        integration_surface="onlyoffice-document-server",
        file_id="file-991",
        file_version="171",
    )
    canonical_file = NexaCanonicalFileSnapshot(
        scope=NexaScopeContext(
            tenant_id="example.com",
            integration_surface="nextcloud-files",
            file_id="file-991",
            file_version="171",
        ),
        content="canonical content from webdav",
        etag='"etag-171"',
        acl_principals=("clay",),
        acl_complete=True,
    )
    candidate = NexaFtsCandidateHit(
        scope=NexaScopeContext(
            tenant_id="example.com",
            integration_surface="nextcloud-files",
            file_id="file-991",
            file_version="171",
        ),
        document_id="fts-doc-991",
        indexed_at="2026-04-19T12:00:00+00:00",
        acl_complete=True,
        acl_principals=("clay",),
        content=base64.b64encode(b"fts excerpt").decode("ascii"),
    )

    plan = resolve_retrieval_plan(
        request_scope,
        canonical_file=canonical_file,
        fts_bootstrap=NexaFtsBootstrapState(
            enabled=True,
            collection_initialized=True,
            privileged_auth=True,
            done_acknowledged=True,
            tombstones_enabled=True,
            content_encoding="base64",
        ),
        fts_candidate=candidate,
    )

    assert plan.canonical_content_source == "webdav"
    assert plan.canonical_content == "canonical content from webdav"
    assert plan.usage.action == "proceed"
    assert plan.usage.reason == "fresh_canonical_file"
    assert plan.candidate_decision.allowed is True
    assert plan.candidate_decision.pointer is not None
    assert plan.candidate_decision.pointer.decoded_content == "fts excerpt"


@pytest.mark.parametrize(
    ("bootstrap", "expected_reason"),
    [
        (
            NexaFtsBootstrapState(
                enabled=True,
                collection_initialized=False,
                privileged_auth=True,
                done_acknowledged=True,
                tombstones_enabled=True,
            ),
            "fts_collection_not_initialized",
        ),
        (
            NexaFtsBootstrapState(
                enabled=True,
                collection_initialized=True,
                privileged_auth=False,
                done_acknowledged=True,
                tombstones_enabled=True,
            ),
            "fts_privileged_auth_required",
        ),
        (
            NexaFtsBootstrapState(
                enabled=True,
                collection_initialized=True,
                privileged_auth=True,
                done_acknowledged=False,
                tombstones_enabled=True,
            ),
            "fts_done_ack_pending",
        ),
        (
            NexaFtsBootstrapState(
                enabled=True,
                collection_initialized=True,
                privileged_auth=True,
                done_acknowledged=True,
                tombstones_enabled=False,
            ),
            "fts_tombstone_contract_incomplete",
        ),
    ],
)
def test_fts_candidate_requires_complete_bootstrap_contract(
    bootstrap: NexaFtsBootstrapState,
    expected_reason: str,
) -> None:
    decision = evaluate_fts_candidate_pointer(
        bootstrap,
        NexaFtsCandidateHit(
            scope=NexaScopeContext(
                tenant_id="example.com",
                integration_surface="nextcloud-files",
                file_id="file-991",
                file_version="171",
            ),
            document_id="fts-doc-991",
            indexed_at="2026-04-19T12:00:00+00:00",
            acl_complete=True,
            acl_principals=("clay",),
        ),
        canonical_scope=NexaScopeContext(
            tenant_id="example.com",
            integration_surface="nextcloud-files",
            file_id="file-991",
            file_version="171",
        ),
    )

    assert decision.allowed is False
    assert decision.reason == expected_reason
    assert decision.pointer is None


def test_fts_candidate_blocks_acl_incomplete_tombstone_and_bad_base64_content() -> None:
    canonical_scope = NexaScopeContext(
        tenant_id="example.com",
        integration_surface="nextcloud-files",
        file_id="file-991",
        file_version="171",
    )
    bootstrap = NexaFtsBootstrapState(
        enabled=True,
        collection_initialized=True,
        privileged_auth=True,
        done_acknowledged=True,
        tombstones_enabled=True,
        content_encoding="base64",
    )

    acl_incomplete = evaluate_fts_candidate_pointer(
        bootstrap,
        NexaFtsCandidateHit(
            scope=canonical_scope,
            document_id="fts-doc-991",
            indexed_at="2026-04-19T12:00:00+00:00",
            acl_complete=False,
            acl_principals=None,
        ),
        canonical_scope=canonical_scope,
    )
    tombstone = evaluate_fts_candidate_pointer(
        bootstrap,
        NexaFtsCandidateHit(
            scope=canonical_scope,
            document_id="fts-doc-991",
            indexed_at="2026-04-19T12:00:00+00:00",
            acl_complete=True,
            acl_principals=("clay",),
            tombstone=True,
        ),
        canonical_scope=canonical_scope,
    )
    bad_decode = evaluate_fts_candidate_pointer(
        bootstrap,
        NexaFtsCandidateHit(
            scope=canonical_scope,
            document_id="fts-doc-991",
            indexed_at="2026-04-19T12:00:00+00:00",
            acl_complete=True,
            acl_principals=("clay",),
            content="not-base64!!!",
        ),
        canonical_scope=canonical_scope,
    )

    assert acl_incomplete.allowed is False
    assert acl_incomplete.reason == "fts_acl_metadata_incomplete"
    assert tombstone.allowed is False
    assert tombstone.reason == "fts_candidate_is_tombstone"
    assert bad_decode.allowed is False
    assert bad_decode.reason == "fts_content_decode_failed"


def test_retrieval_gate_reschedules_stale_or_acl_incomplete_file_usage() -> None:
    stale_decision = evaluate_retrieval_gate(
        NexaScopeContext(
            tenant_id="example.com",
            integration_surface="onlyoffice-document-server",
            file_id="file-991",
            file_version="171",
        ),
        canonical_file=NexaCanonicalFileSnapshot(
            scope=NexaScopeContext(
                tenant_id="example.com",
                integration_surface="nextcloud-files",
                file_id="file-991",
                file_version="172",
            ),
            content="canonical content from webdav",
            etag='"etag-172"',
            acl_principals=("clay",),
            acl_complete=True,
        ),
    )
    acl_incomplete_decision = evaluate_retrieval_gate(
        NexaScopeContext(
            tenant_id="example.com",
            integration_surface="onlyoffice-document-server",
            file_id="file-991",
            file_version="172",
        ),
        canonical_file=NexaCanonicalFileSnapshot(
            scope=NexaScopeContext(
                tenant_id="example.com",
                integration_surface="nextcloud-files",
                file_id="file-991",
                file_version="172",
            ),
            content="canonical content from webdav",
            etag='"etag-172"',
            acl_principals=(),
            acl_complete=False,
        ),
    )

    assert stale_decision.action == "reschedule"
    assert stale_decision.reason == "stale_file_version"
    assert stale_decision.expected_file_version == "171"
    assert stale_decision.current_file_version == "172"
    assert acl_incomplete_decision.action == "reschedule"
    assert acl_incomplete_decision.reason == "canonical_acl_metadata_incomplete"


def test_retrieval_gate_can_cancel_stale_jobs_when_requested() -> None:
    decision = evaluate_retrieval_gate(
        NexaScopeContext(
            tenant_id="example.com",
            integration_surface="onlyoffice-document-server",
            file_id="file-991",
            file_version="171",
        ),
        canonical_file=NexaCanonicalFileSnapshot(
            scope=NexaScopeContext(
                tenant_id="example.com",
                integration_surface="nextcloud-files",
                file_id="file-991",
                file_version="173",
            ),
            content="canonical content from webdav",
            etag='"etag-173"',
            acl_principals=("clay",),
            acl_complete=True,
        ),
        stale_action="cancel",
    )

    assert decision.action == "cancel"
    assert decision.reason == "stale_file_version"
