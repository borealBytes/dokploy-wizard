"""ACL-safe retrieval policy helpers for Nexa file work."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

from .nexa_scope import NexaScopeContext  # pyright: ignore[reportMissingImports]

NexaRetrievalAction = Literal["proceed", "cancel", "reschedule"]
NexaCanonicalContentSource = Literal["webdav"]
NexaFtsContentEncoding = Literal["plain", "base64"]


@dataclass(frozen=True)
class NexaCanonicalFileSnapshot:
    """Canonical file content and metadata read from WebDAV."""

    scope: NexaScopeContext
    content: str
    etag: str
    acl_principals: tuple[str, ...]
    acl_complete: bool

    def __post_init__(self) -> None:
        if self.scope.file_id is None:
            msg = "Canonical WebDAV snapshot requires an explicit file_id boundary."
            raise ValueError(msg)
        if self.etag.strip() == "":
            msg = "Canonical WebDAV snapshot requires a non-empty etag."
            raise ValueError(msg)


@dataclass(frozen=True)
class NexaFtsBootstrapState:
    """Explicit FTS Collection API bootstrap and trust assumptions."""

    enabled: bool
    collection_initialized: bool = False
    privileged_auth: bool = False
    done_acknowledged: bool = False
    tombstones_enabled: bool = False
    content_encoding: NexaFtsContentEncoding = "base64"


@dataclass(frozen=True)
class NexaFtsCandidateHit:
    """Candidate hit surfaced by FTS before WebDAV revalidation."""

    scope: NexaScopeContext
    document_id: str
    indexed_at: str
    acl_complete: bool
    acl_principals: tuple[str, ...] | None = None
    tombstone: bool = False
    content: str | None = None

    def __post_init__(self) -> None:
        if self.scope.file_id is None:
            msg = "FTS candidate hit requires an explicit file_id boundary."
            raise ValueError(msg)
        if self.document_id.strip() == "":
            msg = "FTS candidate hit requires a non-empty document id."
            raise ValueError(msg)
        if self.indexed_at.strip() == "":
            msg = "FTS candidate hit requires a non-empty indexed_at marker."
            raise ValueError(msg)


@dataclass(frozen=True)
class NexaFtsCandidatePointer:
    """Allowed FTS pointer data after ACL and freshness revalidation."""

    scope: NexaScopeContext
    document_id: str
    acl_principals: tuple[str, ...]
    decoded_content: str | None
    source: str = "fts-collection"


@dataclass(frozen=True)
class NexaUsageDecision:
    """Whether downstream work may safely proceed, cancel, or reschedule."""

    action: NexaRetrievalAction
    reason: str
    expected_file_version: str | None
    current_file_version: str | None


@dataclass(frozen=True)
class NexaCandidatePointerDecision:
    """Result of validating an FTS hit as a non-canonical retrieval pointer."""

    allowed: bool
    reason: str
    pointer: NexaFtsCandidatePointer | None


@dataclass(frozen=True)
class NexaRetrievalPlan:
    """Resolved retrieval plan that preserves canonical-source precedence."""

    canonical_content_source: NexaCanonicalContentSource
    canonical_file: NexaCanonicalFileSnapshot
    usage: NexaUsageDecision
    candidate_decision: NexaCandidatePointerDecision

    @property
    def canonical_content(self) -> str:
        return self.canonical_file.content


def evaluate_retrieval_gate(
    request_scope: NexaScopeContext,
    *,
    canonical_file: NexaCanonicalFileSnapshot,
    stale_action: NexaRetrievalAction = "reschedule",
) -> NexaUsageDecision:
    """Require explicit file/version and canonical ACL metadata before proceeding."""

    _require_file_boundary_match(request_scope, canonical_file.scope)
    if not canonical_file.acl_complete or len(canonical_file.acl_principals) == 0:
        return NexaUsageDecision(
            action="reschedule",
            reason="canonical_acl_metadata_incomplete",
            expected_file_version=request_scope.file_version,
            current_file_version=canonical_file.scope.file_version,
        )

    expected_version = request_scope.file_version
    current_version = canonical_file.scope.file_version
    if expected_version is None:
        return NexaUsageDecision(
            action="reschedule",
            reason="missing_expected_file_version_boundary",
            expected_file_version=None,
            current_file_version=current_version,
        )
    if current_version is None:
        return NexaUsageDecision(
            action="reschedule",
            reason="missing_canonical_file_version",
            expected_file_version=expected_version,
            current_file_version=None,
        )
    if expected_version != current_version:
        return NexaUsageDecision(
            action=stale_action,
            reason="stale_file_version",
            expected_file_version=expected_version,
            current_file_version=current_version,
        )
    return NexaUsageDecision(
        action="proceed",
        reason="fresh_canonical_file",
        expected_file_version=expected_version,
        current_file_version=current_version,
    )


def evaluate_fts_candidate_pointer(
    bootstrap: NexaFtsBootstrapState,
    candidate: NexaFtsCandidateHit,
    *,
    canonical_scope: NexaScopeContext,
) -> NexaCandidatePointerDecision:
    """Allow FTS hits only as revalidated pointers, never as canonical content."""

    _require_file_boundary_match(candidate.scope, canonical_scope)
    if not bootstrap.enabled:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_disabled",
            pointer=None,
        )
    if not bootstrap.collection_initialized:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_collection_not_initialized",
            pointer=None,
        )
    if not bootstrap.privileged_auth:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_privileged_auth_required",
            pointer=None,
        )
    if not bootstrap.done_acknowledged:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_done_ack_pending",
            pointer=None,
        )
    if not bootstrap.tombstones_enabled:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_tombstone_contract_incomplete",
            pointer=None,
        )
    if candidate.tombstone:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_candidate_is_tombstone",
            pointer=None,
        )
    if not candidate.acl_complete or not candidate.acl_principals:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_acl_metadata_incomplete",
            pointer=None,
        )
    if (
        candidate.scope.file_version is not None
        and canonical_scope.file_version is not None
        and candidate.scope.file_version != canonical_scope.file_version
    ):
        return NexaCandidatePointerDecision(
            allowed=False,
            reason="fts_candidate_version_stale",
            pointer=None,
        )
    decoded_content, decode_error = _decode_candidate_content(
        content=candidate.content,
        encoding=bootstrap.content_encoding,
    )
    if decode_error is not None:
        return NexaCandidatePointerDecision(
            allowed=False,
            reason=decode_error,
            pointer=None,
        )
    return NexaCandidatePointerDecision(
        allowed=True,
        reason="fts_candidate_pointer_allowed",
        pointer=NexaFtsCandidatePointer(
            scope=candidate.scope,
            document_id=candidate.document_id,
            acl_principals=candidate.acl_principals,
            decoded_content=decoded_content,
        ),
    )


def resolve_retrieval_plan(
    request_scope: NexaScopeContext,
    *,
    canonical_file: NexaCanonicalFileSnapshot,
    fts_bootstrap: NexaFtsBootstrapState | None = None,
    fts_candidate: NexaFtsCandidateHit | None = None,
    stale_action: NexaRetrievalAction = "reschedule",
) -> NexaRetrievalPlan:
    """Resolve safe retrieval with WebDAV precedence and optional FTS pointers."""

    usage = evaluate_retrieval_gate(
        request_scope,
        canonical_file=canonical_file,
        stale_action=stale_action,
    )
    candidate_decision = NexaCandidatePointerDecision(
        allowed=False,
        reason="no_fts_candidate",
        pointer=None,
    )
    if usage.action == "proceed" and fts_bootstrap is not None and fts_candidate is not None:
        candidate_decision = evaluate_fts_candidate_pointer(
            fts_bootstrap,
            fts_candidate,
            canonical_scope=canonical_file.scope,
        )
    return NexaRetrievalPlan(
        canonical_content_source="webdav",
        canonical_file=canonical_file,
        usage=usage,
        candidate_decision=candidate_decision,
    )


def _decode_candidate_content(
    *, content: str | None, encoding: NexaFtsContentEncoding
) -> tuple[str | None, str | None]:
    if content is None:
        return None, None
    if encoding == "plain":
        return content, None
    try:
        decoded = base64.b64decode(content.encode("ascii"), validate=True)
    except ValueError:
        return None, "fts_content_decode_failed"
    try:
        return decoded.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "fts_content_decode_failed"


def _require_file_boundary_match(left: NexaScopeContext, right: NexaScopeContext) -> None:
    if left.file_id is None or right.file_id is None:
        msg = "Retrieval policy requires explicit file_id boundaries on both scopes."
        raise ValueError(msg)
    if left.tenant_id != right.tenant_id or left.file_id != right.file_id:
        msg = "Retrieval policy requires matching tenant/file boundaries."
        raise ValueError(msg)


__all__ = [
    "NexaCanonicalFileSnapshot",
    "NexaCandidatePointerDecision",
    "NexaFtsBootstrapState",
    "NexaFtsCandidateHit",
    "NexaFtsCandidatePointer",
    "NexaRetrievalPlan",
    "NexaUsageDecision",
    "evaluate_fts_candidate_pointer",
    "evaluate_retrieval_gate",
    "resolve_retrieval_plan",
]
