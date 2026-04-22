"""Scoped Mem0 config mapping and Nexa memory write policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, Mapping

from .nexa_scope import NexaScopeContext  # pyright: ignore[reportMissingImports]

DEFAULT_NEXA_MEM0_EMBEDDER_MODEL = "BAAI/bge-small-en-v1.5"
PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS = 384
DEFAULT_NEXA_MEM0_ENTITY_COLLECTION = "mem0_entities"

NexaMemoryLayer = Literal["session", "user", "shared", "episodic", "durable_facts"]
NexaMemoryVisibility = Literal["private", "shared"]


class NexaMemoryConfigError(ValueError):
    """Raised when Mem0 configuration is incomplete or violates the contract."""


@dataclass(frozen=True)
class NexaMem0LlmConfig:
    """OpenAI-compatible LLM config for Mem0 operations against NVIDIA."""

    provider: str
    base_url: str
    api_key: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "config": {
                "base_url": self.base_url,
                "api_key": self.api_key,
            },
        }


@dataclass(frozen=True)
class NexaMem0EmbedderConfig:
    """Pinned local embedder contract for memory writes and retrieval."""

    provider: str
    model: str
    dimensions: int

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "config": {
                "model": self.model,
                "embedding_dims": self.dimensions,
            },
        }


@dataclass(frozen=True)
class NexaMem0VectorConfig:
    """Pinned vector-store contract aligned to the embedder schema."""

    provider: str
    base_url: str
    api_key: str
    embedding_model_dims: int

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "config": {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "embedding_model_dims": self.embedding_model_dims,
            },
        }


@dataclass(frozen=True)
class NexaMem0RestSecurity:
    """Explicit security contract for self-hosted Mem0 REST exposure."""

    require_private_network: bool
    require_api_key_auth: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "require_private_network": self.require_private_network,
            "require_api_key_auth": self.require_api_key_auth,
        }


@dataclass(frozen=True)
class NexaMem0Config:
    """Resolved Mem0 config for the narrow Nexa adapter layer."""

    base_url: str
    api_key: str
    llm: NexaMem0LlmConfig
    embedder: NexaMem0EmbedderConfig
    vector_store: NexaMem0VectorConfig
    rest_security: NexaMem0RestSecurity
    entity_collection: str = DEFAULT_NEXA_MEM0_ENTITY_COLLECTION

    def to_mem0_config(self) -> dict[str, object]:
        return {
            "api_key": self.api_key,
            "host": self.base_url,
            "llm": self.llm.to_dict(),
            "embedder": self.embedder.to_dict(),
            "vector_store": self.vector_store.to_dict(),
            "rest_security": self.rest_security.to_dict(),
            "entity_collection": {
                "enabled": True,
                "name": self.entity_collection,
            },
        }


@dataclass(frozen=True)
class NexaMemoryNamespace:
    """One explicit memory namespace with persistence and visibility boundaries."""

    layer: NexaMemoryLayer
    namespace: str
    visibility: NexaMemoryVisibility
    durable: bool


@dataclass(frozen=True)
class NexaMemoryScopes:
    """Layered session and long-term memory boundaries for one Nexa work item."""

    session_memory: NexaMemoryNamespace
    user_memory: NexaMemoryNamespace | None
    shared_memory: NexaMemoryNamespace | None
    episodic_memory: NexaMemoryNamespace
    durable_facts_memory: NexaMemoryNamespace


@dataclass(frozen=True)
class NexaMemoryWriteRequest:
    """Candidate memory write before policy filtering."""

    scope: NexaScopeContext
    target_layer: NexaMemoryLayer
    content: str
    content_class: str
    visibility: NexaMemoryVisibility
    contains_private_memory: bool = False
    allow_private_to_shared: bool = False


@dataclass(frozen=True)
class NexaMemoryWriteDecision:
    """Policy result for a candidate memory write."""

    allowed: bool
    reason: str
    target_layer: NexaMemoryLayer | None


def build_nexa_mem0_config(env: Mapping[str, str]) -> NexaMem0Config:
    """Map the OpenClaw Nexa env contract into a narrow Mem0 config object."""

    embedder_dimensions = _parse_pinned_dimensions(
        env,
        key="OPENCLAW_NEXA_MEM0_EMBEDDER_DIMENSIONS",
        default=PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS,
    )
    vector_dimensions = _parse_pinned_dimensions(
        env,
        key="OPENCLAW_NEXA_MEM0_VECTOR_DIMENSIONS",
        default=PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS,
    )
    return NexaMem0Config(
        base_url=_require_env(env, "OPENCLAW_NEXA_MEM0_BASE_URL"),
        api_key=_require_env(env, "OPENCLAW_NEXA_MEM0_API_KEY"),
        llm=NexaMem0LlmConfig(
            provider="openai",
            base_url=_require_env(env, "OPENCLAW_NEXA_MEM0_LLM_BASE_URL"),
            api_key=_require_env(env, "OPENCLAW_NEXA_MEM0_LLM_API_KEY"),
        ),
        embedder=NexaMem0EmbedderConfig(
            provider="huggingface",
            model=env.get("OPENCLAW_NEXA_MEM0_EMBEDDER_MODEL", DEFAULT_NEXA_MEM0_EMBEDDER_MODEL),
            dimensions=embedder_dimensions,
        ),
        vector_store=NexaMem0VectorConfig(
            provider=_require_env(env, "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND"),
            base_url=_require_env(env, "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL"),
            api_key=_require_env(env, "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY"),
            embedding_model_dims=vector_dimensions,
        ),
        rest_security=NexaMem0RestSecurity(
            require_private_network=True,
            require_api_key_auth=True,
        ),
    )


def build_nexa_memory_scopes(
    scope: NexaScopeContext,
    *,
    today: date | None = None,
) -> NexaMemoryScopes:
    """Build explicit layered namespaces from the Task 6 scope contract."""

    current_day = today or datetime.now(tz=UTC).date()
    session_namespace = _join_namespace(
        scope.queue_scope_key(),
        "session:active",
    )
    user_memory = None
    if scope.user_id is not None:
        user_memory = NexaMemoryNamespace(
            layer="user",
            namespace=_join_namespace(f"tenant:{scope.tenant_id}", f"user:{scope.user_id}"),
            visibility="private",
            durable=True,
        )
    shared_memory = _shared_memory_namespace(scope)
    episodic_memory = NexaMemoryNamespace(
        layer="episodic",
        namespace=_join_namespace(_durable_context_namespace(scope), f"date:{current_day.isoformat()}"),
        visibility="shared" if shared_memory is not None else "private",
        durable=True,
    )
    durable_facts_memory = NexaMemoryNamespace(
        layer="durable_facts",
        namespace=_join_namespace(_durable_context_namespace(scope), "facts"),
        visibility="shared" if shared_memory is not None else "private",
        durable=True,
    )
    return NexaMemoryScopes(
        session_memory=NexaMemoryNamespace(
            layer="session",
            namespace=session_namespace,
            visibility="private",
            durable=False,
        ),
        user_memory=user_memory,
        shared_memory=shared_memory,
        episodic_memory=episodic_memory,
        durable_facts_memory=durable_facts_memory,
    )


def evaluate_memory_write_policy(request: NexaMemoryWriteRequest) -> NexaMemoryWriteDecision:
    """Apply the conservative write-policy rules for Nexa memory."""

    if request.content.strip() == "":
        return NexaMemoryWriteDecision(
            allowed=False,
            reason="empty_content",
            target_layer=None,
        )
    if request.content_class in {"callback_noise", "retry_noise"}:
        return NexaMemoryWriteDecision(
            allowed=False,
            reason=f"{request.content_class}_is_not_memory",
            target_layer=None,
        )
    if request.target_layer != "session" and request.content_class in {
        "raw_room_transcript",
        "retrieval_result",
        "background_output",
        "briefing_output",
    }:
        return NexaMemoryWriteDecision(
            allowed=False,
            reason=f"{request.content_class}_is_not_durable",
            target_layer=None,
        )
    if request.target_layer == "session" and request.content_class in {
        "background_output",
        "briefing_output",
    }:
        return NexaMemoryWriteDecision(
            allowed=False,
            reason="background_and_briefing_outputs_do_not_pollute_interactive_memory",
            target_layer=None,
        )
    if (
        request.visibility == "shared"
        and request.contains_private_memory
        and not request.allow_private_to_shared
    ):
        return NexaMemoryWriteDecision(
            allowed=False,
            reason="private_memory_requires_explicit_shared_output_opt_in",
            target_layer=None,
        )
    return NexaMemoryWriteDecision(
        allowed=True,
        reason="allowed",
        target_layer=request.target_layer,
    )


def _require_env(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if value is None or value.strip() == "":
        msg = f"Missing required Nexa Mem0 env key '{key}'."
        raise NexaMemoryConfigError(msg)
    return value


def _parse_pinned_dimensions(env: Mapping[str, str], *, key: str, default: int) -> int:
    raw_value = env.get(key)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except ValueError as error:
        msg = f"Expected integer dimensions for '{key}', found {raw_value!r}."
        raise NexaMemoryConfigError(msg) from error
    if parsed != PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS:
        msg = (
            f"{key} must stay pinned to {PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS} for "
            "BAAI/bge-small-en-v1.5."
        )
        raise NexaMemoryConfigError(msg)
    return parsed


def _shared_memory_namespace(scope: NexaScopeContext) -> NexaMemoryNamespace | None:
    if scope.room_id is not None:
        return NexaMemoryNamespace(
            layer="shared",
            namespace=_join_namespace(
                f"tenant:{scope.tenant_id}",
                f"surface:{scope.integration_surface}",
                f"room:{scope.room_id}",
            ),
            visibility="shared",
            durable=True,
        )
    if scope.file_id is not None:
        return NexaMemoryNamespace(
            layer="shared",
            namespace=_join_namespace(
                f"tenant:{scope.tenant_id}",
                f"surface:{scope.integration_surface}",
                f"project:{scope.file_id}",
            ),
            visibility="shared",
            durable=True,
        )
    return None


def _durable_context_namespace(scope: NexaScopeContext) -> str:
    if scope.room_id is not None:
        return _join_namespace(
            f"tenant:{scope.tenant_id}",
            f"surface:{scope.integration_surface}",
            f"room:{scope.room_id}",
            *(tuple() if scope.thread_id is None else (f"thread:{scope.thread_id}",)),
        )
    if scope.file_id is not None:
        parts = [
            f"tenant:{scope.tenant_id}",
            f"surface:{scope.integration_surface}",
            f"project:{scope.file_id}",
        ]
        if scope.file_version is not None:
            parts.append(f"version:{scope.file_version}")
        return _join_namespace(*parts)
    if scope.user_id is not None:
        return _join_namespace(f"tenant:{scope.tenant_id}", f"user:{scope.user_id}")
    return _join_namespace(f"tenant:{scope.tenant_id}", f"surface:{scope.integration_surface}")


def _join_namespace(*parts: str) -> str:
    return "|".join(part for part in parts if part)


__all__ = [
    "DEFAULT_NEXA_MEM0_EMBEDDER_MODEL",
    "DEFAULT_NEXA_MEM0_ENTITY_COLLECTION",
    "NexaMem0Config",
    "NexaMem0EmbedderConfig",
    "NexaMem0LlmConfig",
    "NexaMem0RestSecurity",
    "NexaMem0VectorConfig",
    "NexaMemoryConfigError",
    "NexaMemoryLayer",
    "NexaMemoryNamespace",
    "NexaMemoryScopes",
    "NexaMemoryWriteDecision",
    "NexaMemoryWriteRequest",
    "NexaMemoryVisibility",
    "PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS",
    "build_nexa_mem0_config",
    "build_nexa_memory_scopes",
    "evaluate_memory_write_policy",
]
