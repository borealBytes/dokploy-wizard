# pyright: reportMissingImports=false

from __future__ import annotations

from dokploy_wizard.packs.openclaw.nexa_mem0_client import NexaMem0Client
from dokploy_wizard.packs.openclaw.nexa_memory import build_nexa_mem0_config
from tests.nexa_mem0_test_server import mem0_base_url, run_recording_mem0_server


def _mem0_env(base_url: str) -> dict[str, str]:
    return {
        "OPENCLAW_NEXA_MEM0_BASE_URL": base_url,
        "OPENCLAW_NEXA_MEM0_API_KEY": "mem0-api-key",
        "OPENCLAW_NEXA_MEM0_LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "OPENCLAW_NEXA_MEM0_LLM_API_KEY": "nvidia-api-key",
        "OPENCLAW_NEXA_MEM0_VECTOR_BACKEND": "qdrant",
        "OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL": "http://qdrant:6333",
        "OPENCLAW_NEXA_MEM0_VECTOR_API_KEY": "vector-api-key",
    }


def test_mem0_client_configures_searches_and_writes_with_json_api_contract() -> None:
    with run_recording_mem0_server(
        search_results=[
            {
                "id": "mem-lookup-1",
                "memory": "Q2 write-back requires a summary after visible send.",
                "score": 0.91,
                "metadata": {"namespace": "tenant:example.com|surface:nextcloud-talk|room:room-42"},
            }
        ]
    ) as server:
        client = NexaMem0Client(build_nexa_mem0_config(_mem0_env(mem0_base_url(server))))

        search_result = client.search_memories(
            query="Q2 follow-up",
            filters={
                "user_id": "clay",
                "agent_id": "nexa:nextcloud-talk",
                "run_id": "evt-talk-room-42-msg-845-v2",
                "metadata": {"tenant_id": "example.com", "namespace": "room-42"},
            },
        )
        write_result = client.add_memory(
            content="Posted the Q2 write-back summary back to the room.",
            user_id="clay",
            agent_id="nexa:nextcloud-talk",
            run_id="evt-talk-room-42-msg-845-v2",
            metadata={"tenant_id": "example.com", "namespace": "room-42", "layer": "shared"},
        )

    assert search_result.outcome == "ok"
    assert search_result.hits[0].memory_id == "mem-lookup-1"
    assert search_result.hits[0].score == 0.91
    assert write_result.outcome == "ok"
    assert write_result.memory_id == "mem-1"
    assert [request.path for request in server.requests] == ["/configure", "/search", "/memories"]
    assert server.requests[0].headers["X-Api-Key"] == "mem0-api-key"
    assert server.requests[1].body["filters"]["agent_id"] == "nexa:nextcloud-talk"
    assert server.requests[2].body["user_id"] == "clay"
    assert server.requests[2].body["metadata"]["layer"] == "shared"


def test_mem0_client_returns_structured_degraded_result_when_search_fails() -> None:
    with run_recording_mem0_server(
        failure_paths={"/search": (503, {"error": "mem0 unavailable"})}
    ) as server:
        client = NexaMem0Client(build_nexa_mem0_config(_mem0_env(mem0_base_url(server))))

        search_result = client.search_memories(
            query="Q2 follow-up",
            filters={"agent_id": "nexa:nextcloud-talk", "metadata": {"tenant_id": "example.com"}},
        )

    assert search_result.outcome == "degraded"
    assert search_result.error is not None
    assert search_result.error.reason == "http_error"
    assert search_result.error.status_code == 503
    assert search_result.hits == ()
