"""Queued Nexa runtime orchestration with Mem0-backed automatic memory hooks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import difflib
import re
from typing import Any, Callable, Literal, Mapping, Protocol

from dokploy_wizard.state.queue_models import DurableJobRecord
from dokploy_wizard.state.store import load_inbox_event_log, load_outbound_delivery_log

from .nexa_mem0_client import (
    NexaMem0Client,
    NexaMem0DegradedError,
    NexaMem0SearchHit,
    NexaMem0WriteResult,
)
from .nexa_memory import (
    NexaMemoryConfigError,
    NexaMemoryWriteRequest,
    build_nexa_mem0_config,
    build_nexa_memory_scopes,
    evaluate_memory_write_policy,
)
from .nexa_onlyoffice import (
    NexaOnlyofficeAgentIdentity,
    NexaOnlyofficeReconcileDecision,
    NexaOnlyofficeSaveSignal,
    build_onlyoffice_save_signal,
    evaluate_onlyoffice_reconcile_policy,
)
from .nexa_retrieval import NexaCanonicalFileSnapshot, NexaUsageDecision, evaluate_retrieval_gate
from .nexa_scope import NexaScopeContext, build_talk_scope
from .nexa_talk_reply import NexaTalkReplyDispatch, NexaTalkReplyRequest, deliver_talk_reply

NexaRuntimeStatus = Literal["completed", "failed"]
NexaRuntimeMemoryStatus = Literal["ok", "degraded", "skipped"]


class NexaTalkReplyPlanner(Protocol):
    def __call__(self, payload: dict[str, Any], memory: "NexaRuntimeMemoryQueryResult") -> "NexaPlannedTalkReply": ...


class NexaOnlyofficeReconcileExecutor(Protocol):
    def __call__(
        self,
        decision: NexaOnlyofficeReconcileDecision,
        save_signal: NexaOnlyofficeSaveSignal,
        canonical_file: NexaCanonicalFileSnapshot,
        memory: "NexaRuntimeMemoryQueryResult",
    ) -> "NexaOnlyofficeActionResult": ...


@dataclass(frozen=True)
class NexaPlannedTalkReply:
    """Reply generation output consumed by the real runtime worker."""

    text: str
    memory_content: str
    memory_content_class: str = "assistant_summary"
    memory_target_layer: Literal["shared", "durable_facts"] = "shared"
    contains_private_memory: bool = False
    allow_private_to_shared: bool = False


@dataclass(frozen=True)
class NexaOnlyofficeActionResult:
    """Authoritative ONLYOFFICE reconcile side effect result."""

    outcome: Literal["applied", "skipped"]
    authoritative_write: bool
    memory_content: str | None = None
    memory_content_class: str = "durable_fact"
    memory_target_layer: Literal["shared", "durable_facts"] = "durable_facts"
    contains_private_memory: bool = False
    allow_private_to_shared: bool = False


@dataclass(frozen=True)
class NexaNextcloudFileCreateRequest:
    filename: str
    relative_path: str
    content: str


@dataclass(frozen=True)
class NexaNextcloudFileCreateResult:
    filename: str
    relative_path: str
    share_url: str
    diff_text: str


@dataclass(frozen=True)
class NexaTerminalCommandResult:
    command: str
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class NexaRuntimeMemoryHit:
    """Runtime-facing memory hit annotated with namespace evidence."""

    memory_id: str | None
    content: str
    score: float | None
    namespace: str
    layer: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class NexaRuntimeMemoryQueryResult:
    """Automatic runtime memory lookup result before downstream actions."""

    status: NexaRuntimeMemoryStatus
    query: str
    hits: tuple[NexaRuntimeMemoryHit, ...]
    searched_namespaces: tuple[str, ...]
    degraded_error: NexaMem0DegradedError | None = None


@dataclass(frozen=True)
class NexaRuntimeMemoryWriteResult:
    """Automatic runtime memory persistence result after downstream success."""

    status: NexaRuntimeMemoryStatus
    attempted: bool
    target_namespace: str | None
    memory_id: str | None
    decision_reason: str
    degraded_error: NexaMem0DegradedError | None = None


@dataclass(frozen=True)
class NexaTalkRuntimeResult:
    """Structured result for a processed Talk job."""

    kind: str
    scope: NexaScopeContext
    memory_read: NexaRuntimeMemoryQueryResult
    reply_dispatch: NexaTalkReplyDispatch
    memory_write: NexaRuntimeMemoryWriteResult


@dataclass(frozen=True)
class NexaOnlyofficeRuntimeResult:
    """Structured result for a processed ONLYOFFICE reconcile job."""

    kind: str
    scope: NexaScopeContext
    decision: NexaOnlyofficeReconcileDecision
    retrieval_gate: NexaUsageDecision
    memory_read: NexaRuntimeMemoryQueryResult
    action_result: NexaOnlyofficeActionResult
    memory_write: NexaRuntimeMemoryWriteResult


@dataclass(frozen=True)
class NexaQueuedJobResult:
    """Top-level queued worker result including degraded memory evidence."""

    status: NexaRuntimeStatus
    job_id: str
    job_kind: str
    completed_at: str | None
    result: NexaTalkRuntimeResult | NexaOnlyofficeRuntimeResult | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class NexaRuntimeDependencies:
    """Injected adapters for downstream action execution."""

    talk_reply_planner: NexaTalkReplyPlanner
    talk_sender: Callable[[dict[str, Any]], Mapping[str, Any]]
    onlyoffice_agent_identity: NexaOnlyofficeAgentIdentity
    load_canonical_file: Callable[[NexaOnlyofficeSaveSignal], NexaCanonicalFileSnapshot]
    onlyoffice_reconcile_executor: NexaOnlyofficeReconcileExecutor
    nextcloud_file_creator: Callable[[NexaNextcloudFileCreateRequest], NexaNextcloudFileCreateResult] | None = None
    terminal_command_runner: Callable[[str], NexaTerminalCommandResult] | None = None
    mem0_client: NexaMem0Client | None = None


def run_queued_nexa_job(
    job: DurableJobRecord,
    *,
    store: Any,
    env: Mapping[str, str],
    dependencies: NexaRuntimeDependencies,
    now: datetime | None = None,
) -> NexaQueuedJobResult:
    """Run one leased Nexa job and persist terminal queue state."""

    try:
        if job.kind == "nexa.talk.process_message":
            result = process_talk_job(job, store=store, env=env, dependencies=dependencies, now=now)
        elif job.kind == "nexa.onlyoffice.reconcile_saved_document":
            result = process_onlyoffice_job(job, store=store, env=env, dependencies=dependencies)
        else:
            msg = f"Unsupported Nexa job kind '{job.kind}'."
            raise ValueError(msg)
        completed = store.mark_job_completed(job_id=job.job_id, now=now)
        return NexaQueuedJobResult(
            status="completed",
            job_id=job.job_id,
            job_kind=job.kind,
            completed_at=completed.updated_at,
            result=result,
        )
    except Exception as exc:
        failed = store.mark_job_failed(job_id=job.job_id, error_message=str(exc), now=now)
        return NexaQueuedJobResult(
            status="failed",
            job_id=job.job_id,
            job_kind=job.kind,
            completed_at=failed.updated_at,
            error_message=str(exc),
        )


def process_talk_job(
    job: DurableJobRecord,
    *,
    store: Any,
    env: Mapping[str, str],
    dependencies: NexaRuntimeDependencies,
    now: datetime | None = None,
) -> NexaTalkRuntimeResult:
    """Run the Talk worker path with automatic memory read and post-send write."""

    payload = _load_event_payload(store, source="nextcloud-talk", idempotency_key=job.idempotency_key)
    scope = build_talk_scope(payload)
    payload = _augment_talk_payload_with_history(store, payload=payload, scope=scope)
    memory_read = _automatic_memory_search(
        env,
        scope=scope,
        query=_memory_query_from_payload(payload),
        mem0_client=dependencies.mem0_client,
    )
    action_result = _maybe_create_markdown_file(payload, dependencies=dependencies)
    if action_result is not None:
        planned_reply = NexaPlannedTalkReply(
            text=(
                f"Created `{action_result.filename}` in your Nextcloud root. Share link: {action_result.share_url}\n\n"
                f"Diff:\n```diff\n{action_result.diff_text}\n```"
            ),
            memory_content=f"Created and shared Nextcloud file {action_result.relative_path} for the user.",
        )
    else:
        planned_reply = dependencies.talk_reply_planner(payload, memory_read)
    reply_request = NexaTalkReplyRequest(
        scope=scope,
        delivery_key=f"talk-reply:{scope.run_id or job.job_id}",
        conversation_id=str(payload["conversation"]["id"]),
        conversation_token=(
            str(payload["conversation"].get("token")).strip()
            if payload["conversation"].get("token") is not None
            else None
        ),
        reply_to_message_id=str(payload["message"]["id"]),
        text=planned_reply.text,
        capabilities=payload.get("capabilities", {}),
        context=payload.get("context"),
    )
    dispatch = deliver_talk_reply(
        reply_request,
        store=store,
        sender=dependencies.talk_sender,
        now=now,
    )
    if dispatch.outcome == "sent":
        memory_write = _automatic_memory_write(
            env,
            scope=scope,
            target_layer=planned_reply.memory_target_layer,
            content=planned_reply.memory_content,
            content_class=planned_reply.memory_content_class,
            contains_private_memory=planned_reply.contains_private_memory,
            allow_private_to_shared=planned_reply.allow_private_to_shared,
            mem0_client=dependencies.mem0_client,
        )
    else:
        memory_write = NexaRuntimeMemoryWriteResult(
            status="skipped",
            attempted=False,
            target_namespace=None,
            memory_id=None,
            decision_reason="reply_not_visibly_sent",
        )
    return NexaTalkRuntimeResult(
        kind=job.kind,
        scope=scope,
        memory_read=memory_read,
        reply_dispatch=dispatch,
        memory_write=memory_write,
    )


def _maybe_create_markdown_file(
    payload: dict[str, Any],
    *,
    dependencies: NexaRuntimeDependencies,
) -> NexaNextcloudFileCreateResult | None:
    if dependencies.nextcloud_file_creator is None:
        return None
    text = str(payload.get("message", {}).get("text", "")).strip()
    lowered = text.lower()
    if "create" not in lowered or ".md" not in lowered:
        return None
    if "nextcloud" not in lowered and "file" not in lowered and "markdown" not in lowered:
        return None
    match = re.search(r"([A-Za-z0-9_.-]+\.md)", text)
    if match is None:
        return None
    filename = match.group(1)
    content = _default_markdown_content(filename)
    return dependencies.nextcloud_file_creator(
        NexaNextcloudFileCreateRequest(
            filename=filename,
            relative_path=filename,
            content=content,
        )
    )


def _default_markdown_content(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip() or filename
    title = " ".join(word.capitalize() for word in stem.split())
    return f"# {title}\n\nCreated by Nexa in Nextcloud.\n"


def _augment_talk_payload_with_history(
    store: Any,
    payload: dict[str, Any],
    *,
    scope: NexaScopeContext,
) -> dict[str, Any]:
    transcript = _recent_talk_transcript(store, scope=scope, current_payload=payload)
    if not transcript:
        return payload
    augmented = dict(payload)
    augmented["recentConversation"] = transcript
    return augmented


def _memory_query_from_payload(payload: dict[str, Any]) -> str:
    message_text = str(payload.get("message", {}).get("text", "")).strip()
    if message_text == "":
        return ""
    recent = payload.get("recentConversation")
    if not isinstance(recent, list):
        return message_text
    parts: list[str] = []
    for entry in recent[-4:]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", "")).strip()
        text = str(entry.get("text", "")).strip()
        if role == "" or text == "":
            continue
        parts.append(f"{role}: {text}")
    parts.append(f"user: {message_text}")
    return "\n".join(parts)


def _recent_talk_transcript(
    store: Any,
    *,
    scope: NexaScopeContext,
    current_payload: dict[str, Any],
    limit: int = 8,
) -> list[dict[str, str]]:
    entries: list[tuple[str, str, str]] = []
    inbox = load_inbox_event_log(store.state_dir)
    current_message_id = str(current_payload.get("message", {}).get("id", "")).strip()
    for event in inbox.events:
        if event.source != "nextcloud-talk":
            continue
        event_scope = _safe_build_talk_scope(event.parsed_payload)
        if event_scope is None or event_scope.queue_scope_key() != scope.queue_scope_key():
            continue
        message = event.parsed_payload.get("message")
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("id", "")).strip()
        if message_id == current_message_id:
            continue
        text = str(message.get("text", "")).strip()
        if text == "":
            continue
        entries.append((event.received_at, "user", text))

    deliveries = load_outbound_delivery_log(store.state_dir)
    for record in deliveries.records:
        if record.channel != "nextcloud-talk" or record.scope_key != scope.queue_scope_key():
            continue
        text = str(record.payload.get("message", "")).strip()
        if text == "":
            continue
        entries.append((record.created_at, "assistant", text))

    entries.sort(key=lambda item: item[0])
    trimmed = entries[-limit:]
    return [{"role": role, "text": text} for _, role, text in trimmed]


def _safe_build_talk_scope(payload: dict[str, Any]) -> NexaScopeContext | None:
    try:
        return build_talk_scope(payload)
    except Exception:
        return None


def _maybe_run_terminal_command(
    payload: dict[str, Any], *, dependencies: NexaRuntimeDependencies
) -> NexaTerminalCommandResult | None:
    if dependencies.terminal_command_runner is None:
        return None
    command = _extract_terminal_command(str(payload.get("message", {}).get("text", "")).strip())
    if command is None:
        return None
    return dependencies.terminal_command_runner(command)


def _extract_terminal_command(text: str) -> str | None:
    lowered = text.lower()
    if "time in hh:mm:ss" in lowered or "time in hh:mm:ss for me" in lowered:
        return 'date +"%H:%M:%S"'
    if any(phrase in lowered for phrase in ("central usa time", "central standard usa time", "central time")) and any(
        phrase in lowered for phrase in ("time", "date", "right now", "what time", "get the time")
    ):
        return 'TZ="America/Chicago" date +"%H:%M:%S"'
    if "what time is it" in lowered and any(
        phrase in lowered for phrase in ("command line", "terminal", "tool use", "shell")
    ):
        return 'date +"%H:%M:%S"'
    if any(phrase in lowered for phrase in ("terminal", "command line", "tool use", "shell")) and any(
        phrase in lowered for phrase in ("time", "date", "right now")
    ):
        return 'date +"%H:%M:%S"'
    fenced = re.search(r"`([^`]+)`", text)
    if fenced is not None and any(
        phrase in lowered for phrase in ("run", "execute", "command line", "terminal", "shell")
    ):
        candidate = fenced.group(1).strip()
        return candidate or None
    prefixed = re.search(
        r"(?:run|execute)\s+(?:this\s+)?(?:command|cmd)?\s*:?[ \t]+(.+)$",
        text,
        re.IGNORECASE,
    )
    if prefixed is not None:
        candidate = prefixed.group(1).strip()
        return candidate or None
    return None


def _format_terminal_command_reply(result: NexaTerminalCommandResult) -> str:
    parts = [f"Ran terminal command `{result.command}`."]
    parts.append(f"Exit code: {result.exit_code}")
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        parts.append(f"Stdout:\n```text\n{stdout}\n```")
    if stderr:
        parts.append(f"Stderr:\n```text\n{stderr}\n```")
    if not stdout and not stderr:
        parts.append("No output.")
    return "\n\n".join(parts)


def _maybe_answer_date_or_time(
    payload: dict[str, Any],
    *,
    now: datetime | None,
) -> NexaPlannedTalkReply | None:
    text = str(payload.get("message", {}).get("text", "")).strip()
    lowered = text.lower()
    if not any(
        phrase in lowered
        for phrase in (
            "what's the date",
            "what is the date",
            "what day is it",
            "what is today",
            "today's date",
            "todays date",
        )
    ):
        return None
    current = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    pretty = current.strftime("%A, %B %d, %Y").replace(" 0", " ")
    return NexaPlannedTalkReply(
        text=f"Today is {pretty}.",
        memory_content=f"Answered a date/time question with {pretty}.",
        memory_content_class="assistant_summary",
        memory_target_layer="shared",
    )


def _diff_for_new_file(relative_path: str, content: str) -> str:
    diff = difflib.unified_diff(
        [],
        [line + "\n" for line in content.splitlines()],
        fromfile=f"a/{relative_path}",
        tofile=f"b/{relative_path}",
        lineterm="",
    )
    return "\n".join(diff)


def process_onlyoffice_job(
    job: DurableJobRecord,
    *,
    store: Any,
    env: Mapping[str, str],
    dependencies: NexaRuntimeDependencies,
) -> NexaOnlyofficeRuntimeResult:
    """Run the ONLYOFFICE reconcile path with gated automatic memory behavior."""

    payload = _load_event_payload(
        store,
        source="onlyoffice-document-server",
        idempotency_key=job.idempotency_key,
    )
    save_signal = build_onlyoffice_save_signal(payload)
    decision = evaluate_onlyoffice_reconcile_policy(
        save_signal,
        agent_identity=dependencies.onlyoffice_agent_identity,
    )
    if decision.action != "reconcile":
        skipped_memory = NexaRuntimeMemoryQueryResult(
            status="skipped",
            query="",
            hits=(),
            searched_namespaces=(),
        )
        skipped_write = NexaRuntimeMemoryWriteResult(
            status="skipped",
            attempted=False,
            target_namespace=None,
            memory_id=None,
            decision_reason=f"decision_{decision.action}",
        )
        return NexaOnlyofficeRuntimeResult(
            kind=job.kind,
            scope=save_signal.scope,
            decision=decision,
            retrieval_gate=NexaUsageDecision(
                action="cancel",
                reason="reconcile_not_required",
                expected_file_version=save_signal.scope.file_version,
                current_file_version=save_signal.scope.file_version,
            ),
            memory_read=skipped_memory,
            action_result=NexaOnlyofficeActionResult(
                outcome="skipped",
                authoritative_write=False,
                memory_content=None,
            ),
            memory_write=skipped_write,
        )
    canonical_file = dependencies.load_canonical_file(save_signal)
    retrieval_gate = evaluate_retrieval_gate(save_signal.scope, canonical_file=canonical_file)
    if retrieval_gate.action != "proceed":
        skipped_memory = NexaRuntimeMemoryQueryResult(
            status="skipped",
            query="",
            hits=(),
            searched_namespaces=(),
        )
        skipped_write = NexaRuntimeMemoryWriteResult(
            status="skipped",
            attempted=False,
            target_namespace=None,
            memory_id=None,
            decision_reason=f"retrieval_{retrieval_gate.reason}",
        )
        return NexaOnlyofficeRuntimeResult(
            kind=job.kind,
            scope=save_signal.scope,
            decision=decision,
            retrieval_gate=retrieval_gate,
            memory_read=skipped_memory,
            action_result=NexaOnlyofficeActionResult(
                outcome="skipped",
                authoritative_write=False,
                memory_content=None,
            ),
            memory_write=skipped_write,
        )
    memory_read = _automatic_memory_search(
        env,
        scope=save_signal.scope,
        query=_build_onlyoffice_memory_query(save_signal, canonical_file),
        mem0_client=dependencies.mem0_client,
    )
    action_result = dependencies.onlyoffice_reconcile_executor(
        decision,
        save_signal,
        canonical_file,
        memory_read,
    )
    if action_result.outcome == "applied" and action_result.authoritative_write:
        memory_write = _automatic_memory_write(
            env,
            scope=save_signal.scope,
            target_layer=action_result.memory_target_layer,
            content=action_result.memory_content or "",
            content_class=action_result.memory_content_class,
            contains_private_memory=action_result.contains_private_memory,
            allow_private_to_shared=action_result.allow_private_to_shared,
            mem0_client=dependencies.mem0_client,
        )
    else:
        memory_write = NexaRuntimeMemoryWriteResult(
            status="skipped",
            attempted=False,
            target_namespace=None,
            memory_id=None,
            decision_reason="authoritative_reconcile_not_applied",
        )
    return NexaOnlyofficeRuntimeResult(
        kind=job.kind,
        scope=save_signal.scope,
        decision=decision,
        retrieval_gate=retrieval_gate,
        memory_read=memory_read,
        action_result=action_result,
        memory_write=memory_write,
    )


def _automatic_memory_search(
    env: Mapping[str, str],
    *,
    scope: NexaScopeContext,
    query: str,
    mem0_client: NexaMem0Client | None,
) -> NexaRuntimeMemoryQueryResult:
    if query.strip() == "":
        return NexaRuntimeMemoryQueryResult(
            status="skipped",
            query=query,
            hits=(),
            searched_namespaces=(),
        )
    configured_client, degraded_error = _resolve_mem0_client(env, mem0_client=mem0_client)
    if degraded_error is not None or configured_client is None:
        return NexaRuntimeMemoryQueryResult(
            status="degraded",
            query=query,
            hits=(),
            searched_namespaces=(),
            degraded_error=degraded_error,
        )
    scopes = build_nexa_memory_scopes(scope)
    namespaces = [
        scopes.session_memory,
        *(tuple() if scopes.user_memory is None else (scopes.user_memory,)),
        *(tuple() if scopes.shared_memory is None else (scopes.shared_memory,)),
        scopes.episodic_memory,
        scopes.durable_facts_memory,
    ]
    hits: list[NexaRuntimeMemoryHit] = []
    searched_namespaces: list[str] = []
    for namespace in namespaces:
        searched_namespaces.append(namespace.namespace)
        search_result = configured_client.search_memories(
            query=query,
            filters=_build_mem0_filters(scope=scope, namespace=namespace.namespace, layer=namespace.layer),
            limit=5,
        )
        if search_result.outcome == "degraded":
            return NexaRuntimeMemoryQueryResult(
                status="degraded",
                query=query,
                hits=tuple(hits),
                searched_namespaces=tuple(searched_namespaces),
                degraded_error=search_result.error,
            )
        hits.extend(_dedupe_search_hits(search_result.hits, namespace=namespace.namespace, layer=namespace.layer))
    return NexaRuntimeMemoryQueryResult(
        status="ok",
        query=query,
        hits=tuple(hits),
        searched_namespaces=tuple(searched_namespaces),
    )


def _automatic_memory_write(
    env: Mapping[str, str],
    *,
    scope: NexaScopeContext,
    target_layer: Literal["shared", "durable_facts"],
    content: str,
    content_class: str,
    contains_private_memory: bool,
    allow_private_to_shared: bool,
    mem0_client: NexaMem0Client | None,
) -> NexaRuntimeMemoryWriteResult:
    scopes = build_nexa_memory_scopes(scope)
    namespace = scopes.shared_memory if target_layer == "shared" else scopes.durable_facts_memory
    if namespace is None:
        return NexaRuntimeMemoryWriteResult(
            status="skipped",
            attempted=False,
            target_namespace=None,
            memory_id=None,
            decision_reason="target_namespace_unavailable",
        )
    decision = evaluate_memory_write_policy(
        NexaMemoryWriteRequest(
            scope=scope,
            target_layer=target_layer,
            content=content,
            content_class=content_class,
            visibility=namespace.visibility,
            contains_private_memory=contains_private_memory,
            allow_private_to_shared=allow_private_to_shared,
        )
    )
    if decision.allowed is False:
        return NexaRuntimeMemoryWriteResult(
            status="skipped",
            attempted=False,
            target_namespace=namespace.namespace,
            memory_id=None,
            decision_reason=decision.reason,
        )
    configured_client, degraded_error = _resolve_mem0_client(env, mem0_client=mem0_client)
    if degraded_error is not None or configured_client is None:
        return NexaRuntimeMemoryWriteResult(
            status="degraded",
            attempted=True,
            target_namespace=namespace.namespace,
            memory_id=None,
            decision_reason=decision.reason,
            degraded_error=degraded_error,
        )
    write_result = configured_client.add_memory(
        content=content,
        user_id=scope.user_id,
        agent_id=_build_agent_id(scope),
        run_id=scope.run_id,
        metadata=_build_mem0_metadata(
            scope=scope,
            namespace=namespace.namespace,
            layer=namespace.layer,
            content_class=content_class,
            visibility=namespace.visibility,
            durable=namespace.durable,
        ),
    )
    return _to_runtime_write_result(write_result, namespace=namespace.namespace, decision_reason=decision.reason)


def _resolve_mem0_client(
    env: Mapping[str, str],
    *,
    mem0_client: NexaMem0Client | None,
) -> tuple[NexaMem0Client | None, NexaMem0DegradedError | None]:
    if mem0_client is not None:
        return mem0_client, None
    try:
        config = build_nexa_mem0_config(env)
    except NexaMemoryConfigError as exc:
        return None, NexaMem0DegradedError(
            operation="mem0_config",
            reason="misconfigured",
            detail=str(exc),
            retryable=False,
        )
    return NexaMem0Client(config), None


def _load_event_payload(store: Any, *, source: str, idempotency_key: str) -> dict[str, Any]:
    event = store.get_incoming_event(source=source, idempotency_key=idempotency_key)
    if event is None:
        msg = f"Missing inbox event for {source}:{idempotency_key}."
        raise ValueError(msg)
    return event.parsed_payload


def _build_mem0_filters(*, scope: NexaScopeContext, namespace: str, layer: str) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "agent_id": _build_agent_id(scope),
        "metadata": {
            "app": "dokploy_wizard",
            "workspace": "nexa",
            "tenant_id": scope.tenant_id,
            "integration_surface": scope.integration_surface,
            "namespace": namespace,
            "layer": layer,
        },
    }
    if scope.user_id is not None:
        filters["user_id"] = scope.user_id
    if scope.run_id is not None and layer == "session":
        filters["run_id"] = scope.run_id
    if scope.room_id is not None:
        filters["metadata"]["room_id"] = scope.room_id
    if scope.thread_id is not None:
        filters["metadata"]["thread_id"] = scope.thread_id
    if scope.file_id is not None:
        filters["metadata"]["file_id"] = scope.file_id
    if scope.file_version is not None:
        filters["metadata"]["file_version"] = scope.file_version
    return filters


def _build_mem0_metadata(
    *,
    scope: NexaScopeContext,
    namespace: str,
    layer: str,
    content_class: str,
    visibility: str,
    durable: bool,
) -> dict[str, Any]:
    metadata = {
        "app": "dokploy_wizard",
        "workspace": "nexa",
        "tenant_id": scope.tenant_id,
        "integration_surface": scope.integration_surface,
        "namespace": namespace,
        "layer": layer,
        "content_class": content_class,
        "visibility": visibility,
        "durable": durable,
    }
    if scope.room_id is not None:
        metadata["room_id"] = scope.room_id
    if scope.thread_id is not None:
        metadata["thread_id"] = scope.thread_id
    if scope.file_id is not None:
        metadata["file_id"] = scope.file_id
    if scope.file_version is not None:
        metadata["file_version"] = scope.file_version
    return metadata


def _build_agent_id(scope: NexaScopeContext) -> str:
    del scope
    return "nexa"


def _dedupe_search_hits(
    hits: tuple[NexaMem0SearchHit, ...], *, namespace: str, layer: str
) -> list[NexaRuntimeMemoryHit]:
    seen: set[tuple[str | None, str]] = set()
    deduped: list[NexaRuntimeMemoryHit] = []
    for hit in hits:
        identity = (hit.memory_id, hit.content)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(
            NexaRuntimeMemoryHit(
                memory_id=hit.memory_id,
                content=hit.content,
                score=hit.score,
                namespace=namespace,
                layer=layer,
                metadata=hit.metadata,
            )
        )
    return deduped


def _to_runtime_write_result(
    result: NexaMem0WriteResult,
    *,
    namespace: str,
    decision_reason: str,
) -> NexaRuntimeMemoryWriteResult:
    if result.outcome == "degraded":
        return NexaRuntimeMemoryWriteResult(
            status="degraded",
            attempted=True,
            target_namespace=namespace,
            memory_id=None,
            decision_reason=decision_reason,
            degraded_error=result.error,
        )
    return NexaRuntimeMemoryWriteResult(
        status="ok",
        attempted=True,
        target_namespace=namespace,
        memory_id=result.memory_id,
        decision_reason=decision_reason,
    )


def _build_onlyoffice_memory_query(
    save_signal: NexaOnlyofficeSaveSignal,
    canonical_file: NexaCanonicalFileSnapshot,
) -> str:
    parts = [
        f"ONLYOFFICE reconcile {save_signal.document_key}",
        canonical_file.content,
    ]
    if save_signal.path is not None:
        parts.insert(1, save_signal.path)
    return " :: ".join(part for part in parts if part.strip() != "")


__all__ = [
    "NexaOnlyofficeActionResult",
    "NexaPlannedTalkReply",
    "NexaQueuedJobResult",
    "NexaRuntimeDependencies",
    "NexaRuntimeMemoryHit",
    "NexaRuntimeMemoryQueryResult",
    "NexaRuntimeMemoryWriteResult",
    "NexaTalkRuntimeResult",
    "NexaOnlyofficeRuntimeResult",
    "process_onlyoffice_job",
    "process_talk_job",
    "run_queued_nexa_job",
]
