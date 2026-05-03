# ruff: noqa: E501
"""Explicit Nexa scope and correlation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

NexaIntegrationSurface = Literal[
    "nextcloud-talk",
    "nextcloud-files",
    "onlyoffice-document-server",
]


def _require_scope_value(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if normalized == "":
        msg = f"Expected non-empty scope value for '{field_name}'."
        raise ValueError(msg)
    return normalized


def _optional_scope_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized != "" else None


def _tenant_from_url(url: str) -> str | None:
    host = urlparse(url).hostname
    if host is None:
        return None
    host = host.strip().lower()
    if host == "":
        return None
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def _parse_onlyoffice_userdata(payload: dict[str, Any]) -> dict[str, Any]:
    userdata = payload.get("userdata")
    if not isinstance(userdata, str) or userdata.strip() == "":
        return {}
    parsed = json.loads(userdata)
    if not isinstance(parsed, dict):
        msg = "Expected ONLYOFFICE userdata to decode to an object."
        raise ValueError(msg)
    return parsed


@dataclass(frozen=True)
class NexaScopeContext:
    """Normalized context axes for a single Nexa work item."""

    tenant_id: str
    integration_surface: NexaIntegrationSurface
    user_id: str | None = None
    room_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    file_id: str | None = None
    file_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _require_scope_value(self.tenant_id, field_name="tenant_id"))
        object.__setattr__(self, "user_id", _optional_scope_value(self.user_id))
        object.__setattr__(self, "room_id", _optional_scope_value(self.room_id))
        object.__setattr__(self, "thread_id", _optional_scope_value(self.thread_id))
        object.__setattr__(self, "run_id", _optional_scope_value(self.run_id))
        object.__setattr__(self, "file_id", _optional_scope_value(self.file_id))
        object.__setattr__(self, "file_version", _optional_scope_value(self.file_version))

    def queue_scope_key(self) -> str:
        """Deterministic queue serialization boundary for this work item."""

        parts = [f"tenant:{self.tenant_id}", f"surface:{self.integration_surface}"]
        if self.room_id is not None:
            parts.append(f"room:{self.room_id}")
            if self.thread_id is not None:
                parts.append(f"thread:{self.thread_id}")
        elif self.file_id is not None:
            parts.append(f"file:{self.file_id}")
        elif self.user_id is not None:
            parts.append(f"user:{self.user_id}")
        return "|".join(parts)

    def correlation_axes(self) -> dict[str, str]:
        """Stable correlation data for later memory/retrieval/reply work."""

        axes = {
            "tenant_id": self.tenant_id,
            "integration_surface": self.integration_surface,
        }
        if self.user_id is not None:
            axes["user_id"] = self.user_id
        if self.room_id is not None:
            axes["room_id"] = self.room_id
        if self.thread_id is not None:
            axes["thread_id"] = self.thread_id
        if self.run_id is not None:
            axes["run_id"] = self.run_id
        if self.file_id is not None:
            axes["file_id"] = self.file_id
        if self.file_version is not None:
            axes["file_version"] = self.file_version
        return axes

    def run_correlation_key(self) -> str | None:
        if self.run_id is None:
            return None
        return "|".join((self.queue_scope_key(), f"run:{self.run_id}"))

    def file_correlation_key(self) -> str | None:
        if self.file_id is None:
            return None
        parts = [f"tenant:{self.tenant_id}", f"file:{self.file_id}"]
        if self.file_version is not None:
            parts.append(f"version:{self.file_version}")
        return "|".join(parts)


def build_talk_scope(payload: dict[str, Any]) -> NexaScopeContext:
    """Extract explicit Talk room/thread scope without hidden defaults."""

    tenant_id = _tenant_from_url(str(payload.get("server", "")))
    if tenant_id is None:
        msg = "Talk payload must include a server URL that resolves to a tenant boundary."
        raise ValueError(msg)

    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        msg = "Talk payload must include a conversation object."
        raise ValueError(msg)
    initiator = payload.get("initiator")
    if not isinstance(initiator, dict):
        msg = "Talk payload must include an initiator object."
        raise ValueError(msg)

    room_id = conversation.get("id")
    if not isinstance(room_id, str) or room_id.strip() == "":
        msg = "Talk payload must include a non-empty conversation id."
        raise ValueError(msg)

    user_id = initiator.get("id")
    if not isinstance(user_id, str) or user_id.strip() == "":
        msg = "Talk payload must include a non-empty initiator id."
        raise ValueError(msg)

    return NexaScopeContext(
        tenant_id=tenant_id,
        integration_surface="nextcloud-talk",
        user_id=user_id,
        room_id=room_id,
        thread_id=extract_talk_thread_id(payload),
        run_id=str(payload.get("webhookEventId", "")),
    )


def build_onlyoffice_scope(payload: dict[str, Any]) -> NexaScopeContext:
    """Extract ONLYOFFICE file scope with explicit version correlation."""

    userdata = _parse_onlyoffice_userdata(payload)
    url = payload.get("url")
    tenant_id = _tenant_from_url(str(url))
    if tenant_id is None:
        msg = "ONLYOFFICE payload must include a URL that resolves to a tenant boundary."
        raise ValueError(msg)

    document_key = payload.get("key")
    if not isinstance(document_key, str) or document_key.strip() == "":
        msg = "ONLYOFFICE payload must include a non-empty key."
        raise ValueError(msg)

    history = payload.get("history")
    file_version: str | None = None
    if isinstance(history, dict):
        server_version = history.get("serverVersion")
        if isinstance(server_version, str) and server_version.strip() != "":
            file_version = server_version

    actions = payload.get("actions")
    user_id: str | None = None
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                candidate = action.get("userid")
                if isinstance(candidate, str) and candidate.strip() != "":
                    user_id = candidate
                    break

    file_id = userdata.get("fileId")
    normalized_file_id = document_key
    if isinstance(file_id, str) and file_id.strip() != "":
        normalized_file_id = file_id

    status = payload.get("status")
    if not isinstance(status, int):
        msg = "ONLYOFFICE payload must include an integer status."
        raise ValueError(msg)

    return NexaScopeContext(
        tenant_id=tenant_id,
        integration_surface="onlyoffice-document-server",
        user_id=user_id,
        run_id=f"{document_key}:status:{status}:version:{file_version or 'unknown'}",
        file_id=normalized_file_id,
        file_version=file_version,
    )


def extract_talk_thread_id(payload: dict[str, Any]) -> str | None:
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("threads") is not True:
        return None
    context = payload.get("context")
    if not isinstance(context, dict):
        return None
    thread_id = context.get("threadId")
    return thread_id if isinstance(thread_id, str) and thread_id.strip() != "" else None


def talk_thread_may_inherit_file_context(
    *,
    talk_scope: NexaScopeContext,
    file_scope: NexaScopeContext,
) -> bool:
    """Allow file context inheritance only on an exact explicit boundary match."""

    if talk_scope.integration_surface != "nextcloud-talk":
        return False
    if file_scope.file_id is None:
        return False
    if talk_scope.tenant_id != file_scope.tenant_id:
        return False
    if talk_scope.room_id is None or file_scope.room_id is None:
        return False
    if talk_scope.room_id != file_scope.room_id:
        return False
    if talk_scope.thread_id is None:
        return False
    return talk_scope.thread_id == file_scope.thread_id


__all__ = [
    "NexaIntegrationSurface",
    "NexaScopeContext",
    "build_onlyoffice_scope",
    "build_talk_scope",
    "extract_talk_thread_id",
    "talk_thread_may_inherit_file_context",
]
