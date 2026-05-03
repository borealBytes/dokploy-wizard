# ruff: noqa: E501
"""ONLYOFFICE save-triggered reconciliation and write-back policy for Nexa v1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from .nexa_scope import (  # pyright: ignore[reportMissingImports]
    NexaScopeContext,
    build_onlyoffice_scope,
)

NexaOnlyofficeProcessingAction = Literal["reconcile", "await_final_close", "ignore"]
NexaOnlyofficeSaveClass = Literal["final_close", "force_save", "non_save_signal"]
NexaOnlyofficeWriteBackMode = Literal["update_original_with_track_changes"]

FINAL_CLOSE_STATUSES = frozenset((2, 3))
FORCE_SAVE_STATUS = 6
TRACKED_EDIT_EXTENSIONS = frozenset(
    {
        ".docx",
        ".doc",
        ".odt",
        ".xlsx",
        ".xls",
        ".ods",
        ".pptx",
        ".ppt",
        ".odp",
    }
)

V1_REJECTED_ASSUMPTIONS = (
    "ONLYOFFICE browser/editor events are optional future work, not a v1 processing requirement.",
    "ONLYOFFICE comment-added callbacks such as onAddComment are not server-to-server webhooks in v1.",
    "Mention lookup or outbound Talk replies are outside this ONLYOFFICE policy surface.",
)
V1_OPTIONAL_FUTURE_INTEGRATIONS = (
    "Optional editor/plugin integrations may enrich later UX, but save-triggered reconciliation remains the required v1 trigger.",
)


@dataclass(frozen=True)
class NexaOnlyofficeAgentIdentity:
    """Real configured Nexa identity used for ONLYOFFICE comments and replies."""

    agent_user_id: str
    display_name: str

    def __post_init__(self) -> None:
        if self.agent_user_id.strip() == "":
            msg = "ONLYOFFICE comment/reply attribution requires a non-empty agent_user_id."
            raise ValueError(msg)
        if self.display_name.strip() == "":
            msg = "ONLYOFFICE comment/reply attribution requires a non-empty display_name."
            raise ValueError(msg)


@dataclass(frozen=True)
class NexaOnlyofficeSaveSignal:
    """Normalized ONLYOFFICE callback data relevant to save-triggered policy."""

    scope: NexaScopeContext
    document_key: str
    status: int
    download_url: str
    changes_url: str | None = None
    force_save_type: int | None = None
    users: tuple[str, ...] = ()
    origin: str | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        if self.scope.integration_surface != "onlyoffice-document-server":
            msg = "ONLYOFFICE save signals require an ONLYOFFICE scope context."
            raise ValueError(msg)
        if self.document_key.strip() == "":
            msg = "ONLYOFFICE save signals require a non-empty document_key."
            raise ValueError(msg)
        if self.download_url.strip() == "":
            msg = "ONLYOFFICE save signals require a non-empty download_url."
            raise ValueError(msg)

    @property
    def save_class(self) -> NexaOnlyofficeSaveClass:
        if self.status in FINAL_CLOSE_STATUSES:
            return "final_close"
        if self.status == FORCE_SAVE_STATUS:
            return "force_save"
        return "non_save_signal"

    @property
    def version_dedupe_key(self) -> str:
        file_key = self.scope.file_correlation_key() or self.scope.queue_scope_key()
        return f"{file_key}|status:{self.status}"


@dataclass(frozen=True)
class NexaOnlyofficeWriteBackPolicy:
    """Explicit v1 write-back contract for ONLYOFFICE-originated document work."""

    mode: NexaOnlyofficeWriteBackMode
    agent_identity: NexaOnlyofficeAgentIdentity
    update_target: str
    track_changes_required: bool
    review_features_likely_supported: bool
    comment_reply_attribution_source: str
    caveats: tuple[str, ...]
    rejected_assumptions: tuple[str, ...] = V1_REJECTED_ASSUMPTIONS
    optional_future_integrations: tuple[str, ...] = V1_OPTIONAL_FUTURE_INTEGRATIONS


@dataclass(frozen=True)
class NexaOnlyofficeReconcileDecision:
    """What the worker should do with one ONLYOFFICE callback signal in v1."""

    action: NexaOnlyofficeProcessingAction
    reason: str
    authoritative: bool
    requires_fresh_canonical_file: bool
    dedupe_key: str
    save_signal: NexaOnlyofficeSaveSignal
    write_back_policy: NexaOnlyofficeWriteBackPolicy


def build_onlyoffice_save_signal(payload: dict[str, Any]) -> NexaOnlyofficeSaveSignal:
    """Normalize ONLYOFFICE callback payload fields used by the v1 policy."""

    scope = build_onlyoffice_scope(payload)
    document_key = payload.get("key")
    if not isinstance(document_key, str) or document_key.strip() == "":
        msg = "ONLYOFFICE payload must include a non-empty key."
        raise ValueError(msg)
    status = payload.get("status")
    if not isinstance(status, int):
        msg = "ONLYOFFICE payload must include an integer status."
        raise ValueError(msg)
    download_url = payload.get("url")
    if not isinstance(download_url, str) or download_url.strip() == "":
        msg = "ONLYOFFICE payload must include a non-empty url."
        raise ValueError(msg)

    changes_url = payload.get("changesurl")
    normalized_changes_url = changes_url if isinstance(changes_url, str) and changes_url.strip() != "" else None
    users = payload.get("users")
    normalized_users = tuple(user for user in users if isinstance(user, str) and user.strip() != "") if isinstance(users, list) else ()
    force_save_type = payload.get("forcesavetype") if isinstance(payload.get("forcesavetype"), int) else None
    userdata = _parse_userdata(payload)

    return NexaOnlyofficeSaveSignal(
        scope=scope,
        document_key=document_key,
        status=status,
        download_url=download_url,
        changes_url=normalized_changes_url,
        force_save_type=force_save_type,
        users=normalized_users,
        origin=_optional_string(userdata.get("origin")),
        path=_optional_string(userdata.get("path")),
    )


def build_onlyoffice_writeback_policy(
    agent_identity: NexaOnlyofficeAgentIdentity,
    *,
    source_path: str | None = None,
    access_mode: str = "authenticated",
    session_mode: str = "editable",
) -> NexaOnlyofficeWriteBackPolicy:
    """Model the explicit v1 decision to update the original document in place."""

    extension = _path_extension(source_path)
    caveats: list[str] = []
    review_features_likely_supported = True
    if access_mode != "authenticated":
        review_features_likely_supported = False
        caveats.append(
            "Public-link or anonymous sessions may not support trusted review/comment attribution for Nexa write-back."
        )
    if session_mode != "editable":
        review_features_likely_supported = False
        caveats.append(
            "Read-only editor sessions cannot be treated as writable review surfaces for tracked Nexa updates."
        )
    if extension is None:
        caveats.append(
            "Unknown source file extensions should be treated cautiously because review/comment capabilities may vary by format."
        )
    elif extension not in TRACKED_EDIT_EXTENSIONS:
        review_features_likely_supported = False
        caveats.append(
            "Some file types may not support Track Changes or comment/reply flows; workers must check format capabilities before write-back."
        )

    return NexaOnlyofficeWriteBackPolicy(
        mode="update_original_with_track_changes",
        agent_identity=agent_identity,
        update_target="original_document",
        track_changes_required=True,
        review_features_likely_supported=review_features_likely_supported,
        comment_reply_attribution_source="configured_agent_identity",
        caveats=tuple(caveats),
    )


def evaluate_onlyoffice_reconcile_policy(
    save_signal: NexaOnlyofficeSaveSignal,
    *,
    agent_identity: NexaOnlyofficeAgentIdentity,
    finalized_versions: frozenset[str] | None = None,
    access_mode: str = "authenticated",
    session_mode: str = "editable",
) -> NexaOnlyofficeReconcileDecision:
    """Resolve the narrow v1 ONLYOFFICE reconciliation decision for one callback."""

    write_back_policy = build_onlyoffice_writeback_policy(
        agent_identity,
        source_path=save_signal.path,
        access_mode=access_mode,
        session_mode=session_mode,
    )
    seen_versions = finalized_versions or frozenset()
    current_version = save_signal.scope.file_version or "unknown"

    if save_signal.status in FINAL_CLOSE_STATUSES:
        if current_version in seen_versions:
            return NexaOnlyofficeReconcileDecision(
                action="ignore",
                reason="duplicate_final_close_save_signal",
                authoritative=True,
                requires_fresh_canonical_file=True,
                dedupe_key=save_signal.version_dedupe_key,
                save_signal=save_signal,
                write_back_policy=write_back_policy,
            )
        return NexaOnlyofficeReconcileDecision(
            action="reconcile",
            reason="final_close_save_signal",
            authoritative=True,
            requires_fresh_canonical_file=True,
            dedupe_key=save_signal.version_dedupe_key,
            save_signal=save_signal,
            write_back_policy=write_back_policy,
        )

    if save_signal.status == FORCE_SAVE_STATUS:
        if current_version in seen_versions:
            reason = "force_save_superseded_by_final_close"
        else:
            reason = "force_save_not_final_truth"
        return NexaOnlyofficeReconcileDecision(
            action="await_final_close",
            reason=reason,
            authoritative=False,
            requires_fresh_canonical_file=True,
            dedupe_key=save_signal.version_dedupe_key,
            save_signal=save_signal,
            write_back_policy=write_back_policy,
        )

    return NexaOnlyofficeReconcileDecision(
        action="ignore",
        reason="non_save_signal",
        authoritative=False,
        requires_fresh_canonical_file=False,
        dedupe_key=save_signal.version_dedupe_key,
        save_signal=save_signal,
        write_back_policy=write_back_policy,
    )


def _parse_userdata(payload: dict[str, Any]) -> dict[str, Any]:
    userdata = payload.get("userdata")
    if not isinstance(userdata, str) or userdata.strip() == "":
        return {}
    decoded = json.loads(userdata)
    if not isinstance(decoded, dict):
        msg = "Expected ONLYOFFICE userdata to decode to an object."
        raise ValueError(msg)
    return decoded


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized != "" else None


def _path_extension(path: str | None) -> str | None:
    if path is None:
        return None
    parsed = urlparse(path)
    raw_path = parsed.path if parsed.scheme != "" else path
    if "." not in raw_path:
        return None
    return "." + raw_path.rsplit(".", 1)[1].lower()


__all__ = [
    "FINAL_CLOSE_STATUSES",
    "FORCE_SAVE_STATUS",
    "TRACKED_EDIT_EXTENSIONS",
    "NexaOnlyofficeAgentIdentity",
    "NexaOnlyofficeReconcileDecision",
    "NexaOnlyofficeSaveSignal",
    "NexaOnlyofficeWriteBackPolicy",
    "build_onlyoffice_save_signal",
    "build_onlyoffice_writeback_policy",
    "evaluate_onlyoffice_reconcile_policy",
]
