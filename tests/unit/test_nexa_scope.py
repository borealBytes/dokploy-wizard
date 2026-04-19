# pyright: reportMissingImports=false

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dokploy_wizard.packs.openclaw.nexa_scope import (
    NexaScopeContext,
    build_onlyoffice_scope,
    build_talk_scope,
    talk_thread_may_inherit_file_context,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _load_json_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_build_talk_scope_extracts_room_thread_run_and_tenant_boundaries() -> None:
    payload = _load_json_fixture("nexa-talk-webhook-room-message.json")

    scope = build_talk_scope(payload)

    assert scope.correlation_axes() == {
        "tenant_id": "example.com",
        "integration_surface": "nextcloud-talk",
        "user_id": "clay",
        "room_id": "room-42",
        "thread_id": "thread-room-42-msg-840",
        "run_id": "evt-talk-room-42-msg-845-v2",
    }
    assert scope.queue_scope_key() == (
        "tenant:example.com|surface:nextcloud-talk|room:room-42|thread:thread-room-42-msg-840"
    )
    assert scope.run_correlation_key() == (
        "tenant:example.com|surface:nextcloud-talk|room:room-42|"
        "thread:thread-room-42-msg-840|run:evt-talk-room-42-msg-845-v2"
    )


def test_build_talk_scope_falls_back_to_room_boundary_when_threads_are_unavailable() -> None:
    payload = _load_json_fixture("nexa-talk-webhook-room-message.json")
    payload["capabilities"]["threads"] = False

    scope = build_talk_scope(payload)

    assert scope.thread_id is None
    assert scope.queue_scope_key() == "tenant:example.com|surface:nextcloud-talk|room:room-42"


def test_build_onlyoffice_scope_extracts_file_and_version_boundaries() -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-2.json")

    scope = build_onlyoffice_scope(payload)

    assert scope.correlation_axes() == {
        "tenant_id": "example.com",
        "integration_surface": "onlyoffice-document-server",
        "user_id": "clay",
        "run_id": "document-key-abc123:status:2:version:171",
        "file_id": "file-991",
        "file_version": "171",
    }
    assert scope.queue_scope_key() == (
        "tenant:example.com|surface:onlyoffice-document-server|file:file-991"
    )
    assert scope.file_correlation_key() == "tenant:example.com|file:file-991|version:171"


def test_build_onlyoffice_scope_falls_back_to_document_key_when_file_id_is_missing() -> None:
    payload = _load_json_fixture("nexa-onlyoffice-callback-status-6.json")
    payload["userdata"] = json.dumps({"origin": "nextcloud"})

    scope = build_onlyoffice_scope(payload)

    assert scope.file_id == "document-key-abc123"
    assert scope.file_version == "172"
    assert scope.queue_scope_key() == (
        "tenant:example.com|surface:onlyoffice-document-server|file:document-key-abc123"
    )


def test_talk_thread_may_inherit_file_context_only_on_exact_boundary_match() -> None:
    talk_scope = NexaScopeContext(
        tenant_id="example.com",
        integration_surface="nextcloud-talk",
        room_id="room-42",
        thread_id="thread-room-42-msg-840",
        run_id="evt-1",
    )
    matching_file_scope = NexaScopeContext(
        tenant_id="example.com",
        integration_surface="nextcloud-files",
        room_id="room-42",
        thread_id="thread-room-42-msg-840",
        file_id="file-991",
    )
    mismatched_thread_scope = NexaScopeContext(
        tenant_id="example.com",
        integration_surface="nextcloud-files",
        room_id="room-42",
        thread_id="thread-room-42-msg-999",
        file_id="file-991",
    )
    mismatched_room_scope = NexaScopeContext(
        tenant_id="example.com",
        integration_surface="nextcloud-files",
        room_id="room-77",
        thread_id="thread-room-42-msg-840",
        file_id="file-991",
    )

    assert talk_thread_may_inherit_file_context(
        talk_scope=talk_scope,
        file_scope=matching_file_scope,
    ) is True
    assert talk_thread_may_inherit_file_context(
        talk_scope=talk_scope,
        file_scope=mismatched_thread_scope,
    ) is False
    assert talk_thread_may_inherit_file_context(
        talk_scope=talk_scope,
        file_scope=mismatched_room_scope,
    ) is False
