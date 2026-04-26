# pyright: reportMissingImports=false

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dokploy_wizard.packs.openclaw.nexa_onlyoffice import (
    FINAL_CLOSE_STATUSES,
    FORCE_SAVE_STATUS,
    NexaOnlyofficeAgentIdentity,
    build_onlyoffice_save_signal,
    build_onlyoffice_writeback_policy,
    evaluate_onlyoffice_reconcile_policy,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _load_json_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_build_onlyoffice_save_signal_extracts_scope_and_save_metadata() -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-2.json")

    save_signal = build_onlyoffice_save_signal(payload)

    assert FINAL_CLOSE_STATUSES == frozenset((2, 3))
    assert FORCE_SAVE_STATUS == 6
    assert save_signal.document_key == "document-key-abc123"
    assert save_signal.save_class == "final_close"
    assert save_signal.scope.file_id == "file-991"
    assert save_signal.scope.file_version == "171"
    assert save_signal.origin == "nextcloud"
    assert save_signal.path == "/Projects/Q2 Plan.docx"
    assert save_signal.users == ("clay", "nexa")
    assert save_signal.version_dedupe_key == "tenant:example.com|file:file-991|version:171|status:2"


@pytest.mark.parametrize("status", [2, 3])
def test_final_close_statuses_trigger_authoritative_reconcile(status: int) -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-2.json")
    payload["status"] = status
    agent_identity = NexaOnlyofficeAgentIdentity(
        agent_user_id="nexa-agent",
        display_name="Nexa",
    )

    decision = evaluate_onlyoffice_reconcile_policy(
        build_onlyoffice_save_signal(payload),
        agent_identity=agent_identity,
    )

    assert decision.action == "reconcile"
    assert decision.reason == "final_close_save_signal"
    assert decision.authoritative is True
    assert decision.requires_fresh_canonical_file is True
    assert decision.write_back_policy.mode == "update_original_with_track_changes"
    assert decision.write_back_policy.update_target == "original_document"
    assert decision.write_back_policy.track_changes_required is True
    assert decision.write_back_policy.comment_reply_attribution_source == "configured_agent_identity"
    assert decision.write_back_policy.agent_identity == agent_identity
    assert decision.write_back_policy.rejected_assumptions == (
        "ONLYOFFICE browser/editor events are optional future work, not a v1 processing requirement.",
        "ONLYOFFICE comment-added callbacks such as onAddComment are not server-to-server webhooks in v1.",
        "Mention lookup or outbound Talk replies are outside this ONLYOFFICE policy surface.",
    )


def test_force_save_stays_non_authoritative_until_final_close_and_dedupes_after_one_arrives() -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-6.json")
    agent_identity = NexaOnlyofficeAgentIdentity(
        agent_user_id="nexa-agent",
        display_name="Nexa",
    )

    first_decision = evaluate_onlyoffice_reconcile_policy(
        build_onlyoffice_save_signal(payload),
        agent_identity=agent_identity,
    )
    deduped_decision = evaluate_onlyoffice_reconcile_policy(
        build_onlyoffice_save_signal(payload),
        agent_identity=agent_identity,
        finalized_versions=frozenset({"172"}),
    )

    assert first_decision.action == "await_final_close"
    assert first_decision.reason == "force_save_not_final_truth"
    assert first_decision.authoritative is False
    assert first_decision.requires_fresh_canonical_file is True
    assert deduped_decision.action == "await_final_close"
    assert deduped_decision.reason == "force_save_superseded_by_final_close"
    assert deduped_decision.authoritative is False


def test_duplicate_final_close_version_is_ignored_after_authoritative_processing() -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-2.json")
    agent_identity = NexaOnlyofficeAgentIdentity(
        agent_user_id="nexa-agent",
        display_name="Nexa",
    )

    decision = evaluate_onlyoffice_reconcile_policy(
        build_onlyoffice_save_signal(payload),
        agent_identity=agent_identity,
        finalized_versions=frozenset({"171"}),
    )

    assert decision.action == "ignore"
    assert decision.reason == "duplicate_final_close_save_signal"
    assert decision.authoritative is True
    assert decision.requires_fresh_canonical_file is True


def test_writeback_policy_adds_review_caveats_for_public_read_only_or_non_review_formats() -> None:
    policy = build_onlyoffice_writeback_policy(
        NexaOnlyofficeAgentIdentity(agent_user_id="nexa-agent", display_name="Nexa"),
        source_path="/Exports/summary.pdf",
        access_mode="public_link",
        session_mode="read_only",
    )

    assert policy.review_features_likely_supported is False
    assert policy.comment_reply_attribution_source == "configured_agent_identity"
    assert policy.caveats == (
        "Public-link or anonymous sessions may not support trusted review/comment attribution for Nexa write-back.",
        "Read-only editor sessions cannot be treated as writable review surfaces for tracked Nexa updates.",
        "Some file types may not support Track Changes or comment/reply flows; workers must check format capabilities before write-back.",
    )


def test_agent_identity_must_be_real_configured_values() -> None:
    with pytest.raises(ValueError, match="agent_user_id"):
        NexaOnlyofficeAgentIdentity(agent_user_id="", display_name="Nexa")
    with pytest.raises(ValueError, match="display_name"):
        NexaOnlyofficeAgentIdentity(agent_user_id="nexa-agent", display_name="")
