# pyright: reportMissingImports=false

from __future__ import annotations

from datetime import date

import pytest

from dokploy_wizard.packs.openclaw.nexa_memory import (
    DEFAULT_NEXA_MEM0_EMBEDDER_MODEL,
    DEFAULT_NEXA_MEM0_ENTITY_COLLECTION,
    PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS,
    NexaMemoryConfigError,
    NexaMemoryWriteRequest,
    build_nexa_mem0_config,
    build_nexa_memory_scopes,
    evaluate_memory_write_policy,
)
from dokploy_wizard.packs.openclaw.nexa_scope import NexaScopeContext


def test_build_nexa_mem0_config_maps_nvidia_through_openai_compatible_settings() -> None:
    config = build_nexa_mem0_config(
        {
            "OPENCLAW_NEXA_MEM0_BASE_URL": "http://mem0:8000",
            "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
            "OPENCLAW_NEXA_MEM0_LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "OPENCLAW_NEXA_MEM0_LLM_API_KEY": "nvidia-api-key",
            "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND": "qdrant",
            "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL": "http://qdrant:6333",
            "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY": "vector-api-key",
            "OPENCLAW_NEXA_MEM0_VECTOR_DIMENSIONS": "384",
        }
    )

    assert config.base_url == "http://mem0:8000"
    assert config.api_key == "mem0-api-key"
    assert config.llm.provider == "openai"
    assert config.llm.base_url == "https://integrate.api.nvidia.com/v1"
    assert config.llm.api_key == "nvidia-api-key"
    assert config.embedder.model == DEFAULT_NEXA_MEM0_EMBEDDER_MODEL
    assert config.embedder.dimensions == PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS
    assert config.vector_store.provider == "qdrant"
    assert config.vector_store.embedding_model_dims == PINNED_NEXA_MEM0_EMBEDDING_DIMENSIONS
    assert config.rest_security.require_private_network is True
    assert config.rest_security.require_api_key_auth is True
    assert config.entity_collection == DEFAULT_NEXA_MEM0_ENTITY_COLLECTION

    assert config.to_mem0_config() == {
        "api_key": "mem0-api-key",
        "host": "http://mem0:8000",
        "llm": {
            "provider": "openai",
            "config": {
                "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": "nvidia-api-key",
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": DEFAULT_NEXA_MEM0_EMBEDDER_MODEL,
                "embedding_dims": 384,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "base_url": "http://qdrant:6333",
                "api_key": "vector-api-key",
                "embedding_model_dims": 384,
            },
        },
        "rest_security": {
            "require_private_network": True,
            "require_api_key_auth": True,
        },
        "entity_collection": {
            "enabled": True,
            "name": DEFAULT_NEXA_MEM0_ENTITY_COLLECTION,
        },
    }


def test_build_nexa_mem0_config_rejects_non_384_dimension_contract() -> None:
    with pytest.raises(NexaMemoryConfigError, match="must stay pinned to 384"):
        build_nexa_mem0_config(
            {
                "OPENCLAW_NEXA_MEM0_BASE_URL": "http://mem0:8000",
                "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
                "OPENCLAW_NEXA_MEM0_LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
                "OPENCLAW_NEXA_MEM0_LLM_API_KEY": "nvidia-api-key",
                "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND": "qdrant",
                "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL": "http://qdrant:6333",
                "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY": "vector-api-key",
                "OPENCLAW_NEXA_MEM0_EMBEDDER_DIMENSIONS": "768",
            }
        )


def test_build_nexa_memory_scopes_keeps_session_and_long_term_layers_explicit() -> None:
    scopes = build_nexa_memory_scopes(
        NexaScopeContext(
            tenant_id="example.com",
            integration_surface="nextcloud-talk",
            user_id="clay",
            room_id="room-42",
            thread_id="thread-room-42-msg-840",
            run_id="evt-talk-room-42-msg-845-v2",
        ),
        today=date(2026, 4, 19),
    )

    assert scopes.session_memory.namespace == (
        "tenant:example.com|surface:nextcloud-talk|room:room-42|thread:thread-room-42-msg-840"
        "|session:evt-talk-room-42-msg-845-v2"
    )
    assert scopes.session_memory.durable is False
    assert scopes.user_memory is not None
    assert scopes.user_memory.namespace == "tenant:example.com|user:clay"
    assert scopes.shared_memory is not None
    assert scopes.shared_memory.namespace == (
        "tenant:example.com|surface:nextcloud-talk|room:room-42"
    )
    assert scopes.episodic_memory.namespace == (
        "tenant:example.com|surface:nextcloud-talk|room:room-42|thread:thread-room-42-msg-840"
        "|date:2026-04-19"
    )
    assert scopes.durable_facts_memory.namespace == (
        "tenant:example.com|surface:nextcloud-talk|room:room-42|thread:thread-room-42-msg-840"
        "|facts"
    )


@pytest.mark.parametrize(
    ("target_layer", "content_class", "visibility", "contains_private_memory", "expected_reason"),
    [
        ("durable_facts", "raw_room_transcript", "shared", False, "raw_room_transcript_is_not_durable"),
        ("durable_facts", "callback_noise", "shared", False, "callback_noise_is_not_memory"),
        ("durable_facts", "retry_noise", "shared", False, "retry_noise_is_not_memory"),
        ("durable_facts", "retrieval_result", "shared", False, "retrieval_result_is_not_durable"),
        (
            "session",
            "briefing_output",
            "private",
            False,
            "background_and_briefing_outputs_do_not_pollute_interactive_memory",
        ),
        (
            "shared",
            "durable_fact",
            "shared",
            True,
            "private_memory_requires_explicit_shared_output_opt_in",
        ),
    ],
)
def test_memory_write_policy_blocks_forbidden_noise_and_private_leakage(
    target_layer: str,
    content_class: str,
    visibility: str,
    contains_private_memory: bool,
    expected_reason: str,
) -> None:
    decision = evaluate_memory_write_policy(
        NexaMemoryWriteRequest(
            scope=NexaScopeContext(
                tenant_id="example.com",
                integration_surface="nextcloud-talk",
                user_id="clay",
                room_id="room-42",
                thread_id="thread-room-42-msg-840",
            ),
            target_layer=target_layer,  # type: ignore[arg-type]
            content="candidate memory",
            content_class=content_class,
            visibility=visibility,  # type: ignore[arg-type]
            contains_private_memory=contains_private_memory,
        )
    )

    assert decision.allowed is False
    assert decision.reason == expected_reason
    assert decision.target_layer is None


def test_memory_write_policy_allows_explicit_structured_fact_write() -> None:
    decision = evaluate_memory_write_policy(
        NexaMemoryWriteRequest(
            scope=NexaScopeContext(
                tenant_id="example.com",
                integration_surface="onlyoffice-document-server",
                user_id="clay",
                file_id="file-991",
                file_version="171",
            ),
            target_layer="durable_facts",
            content="User prefers storing ONLYOFFICE review comments for project file-991.",
            content_class="durable_fact",
            visibility="private",
        )
    )

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.target_layer == "durable_facts"
