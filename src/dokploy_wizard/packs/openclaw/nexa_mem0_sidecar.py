# pyright: reportMissingImports=false
"""Tiny Mem0 REST sidecar for Nexa internal deployments."""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from mem0 import Memory


class ConfigureRequest(BaseModel):
    vector_store: dict[str, Any]
    llm: dict[str, Any]
    embedder: dict[str, Any]
    version: str | None = None
    history_db_path: str | None = None


class SearchRequest(BaseModel):
    query: str
    limit: int = 5
    filters: dict[str, Any] = Field(default_factory=dict)


class AddMemoryRequest(BaseModel):
    memory: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


APP = FastAPI(title="Nexa Mem0 Sidecar")
_API_KEY = os.environ.get("ADMIN_API_KEY", "")
_MEMORY: Memory | None = None


def _require_api_key(x_api_key: str | None) -> None:
    if _API_KEY and x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


@APP.get("/openapi.json")
def openapi_passthrough() -> dict[str, Any]:
    return APP.openapi()


@APP.post("/configure")
def configure(payload: ConfigureRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    global _MEMORY
    _require_api_key(x_api_key)
    config = payload.model_dump(exclude_none=True)
    _MEMORY = Memory.from_config(config)
    return {"configured": True}


@APP.post("/search")
def search(payload: SearchRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_key(x_api_key)
    if _MEMORY is None:
        raise HTTPException(status_code=409, detail="mem0 not configured")
    results = _MEMORY.search(payload.query, limit=payload.limit, filters=payload.filters)
    return {"results": results}


@APP.post("/memories")
def add_memory(payload: AddMemoryRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_key(x_api_key)
    if _MEMORY is None:
        raise HTTPException(status_code=409, detail="mem0 not configured")
    result = _MEMORY.add(
        payload.memory,
        user_id=payload.user_id,
        agent_id=payload.agent_id,
        run_id=payload.run_id,
        metadata=payload.metadata,
    )
    if isinstance(result, dict):
        return result
    return json.loads(json.dumps(result))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(APP, host="0.0.0.0", port=8000)
