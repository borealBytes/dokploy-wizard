#!/usr/bin/env python3
"""Dokploy Wizard LiteLLM model autosync sidecar.

Polls the configured model catalog sources (OpenRouter, OpenCode Zen, NVIDIA),
registers models the gateway does not expose yet through the LiteLLM admin
API, and prunes managed deployments that disappeared from their source.

Managed deployments are tagged with model_info.metadata.managed_by, so only
wizard-created deployments are ever pruned. Config-file models and
operator-created models are never touched.

Environment:
  LITELLM_GATEWAY_URL          internal gateway base URL, e.g. http://host:4000
  LITELLM_MASTER_KEY           LiteLLM master key used for admin API calls
  MODEL_SYNC_SOURCES_FILE      rendered sources JSON document (default /app/sources.json)
  MODEL_SYNC_STATE_DIR         writable directory for run markers (default /state)
  MODEL_SYNC_INTERVAL_MINUTES  minutes between sync cycles (default 360)
  MODEL_SYNC_ONCE              set to 1 to run a single cycle and exit
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib import request

SOURCES_FILE = os.environ.get("MODEL_SYNC_SOURCES_FILE", "/app/sources.json")
STATE_DIR = os.environ.get("MODEL_SYNC_STATE_DIR", "/state")
GATEWAY_URL = os.environ.get("LITELLM_GATEWAY_URL", "http://litellm:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
INTERVAL_MINUTES = float(os.environ.get("MODEL_SYNC_INTERVAL_MINUTES", "360"))
MODEL_SYNC_ONCE = os.environ.get("MODEL_SYNC_ONCE", "0") == "1"
HEALTHCHECK_GRACE_SECONDS = 900
MANAGED_BY = "dokploy-wizard-model-sync"
ALIAS_PREFIX = "sync"
USER_AGENT = "dokploy-wizard-model-sync/1"
REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_MODELS_PER_PROVIDER = 50


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[model-sync] {stamp} {message}", flush=True)


def http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, method=method, data=body)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def load_document() -> dict[str, Any]:
    with open(SOURCES_FILE, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"sources document must be an object: {SOURCES_FILE}")
    return document


def source_model_ids(source: dict[str, Any], max_models: int) -> list[str]:
    api_key_env = source.get("api_key_env")
    token = os.environ.get(api_key_env, "") if isinstance(api_key_env, str) else ""
    payload = http_json("GET", source["list_url"], token=token or None)
    model_ids: list[str] = []
    for item in payload.get("data", []) or []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        if model_id.endswith("/*"):
            continue
        model_ids.append(model_id)
        if len(model_ids) >= max_models:
            break
    return model_ids


def desired_deployments(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    max_models = int(document.get("max_models_per_provider", DEFAULT_MAX_MODELS_PER_PROVIDER))
    deployments: dict[str, dict[str, Any]] = {}
    ok_sources: set[str] = set()
    for source in document.get("sources", []) or []:
        name = str(source.get("name", ""))
        try:
            model_ids = source_model_ids(source, max_models)
        except Exception as exc:
            log(f"source {name or '?'} fetch failed: {type(exc).__name__}: {exc}")
            continue
        ok_sources.add(name)
        prefix = str(source.get("alias_prefix", f"{ALIAS_PREFIX}/{name}"))
        template = str(source.get("model_ref_template", "{model}"))
        for model_id in model_ids:
            params: dict[str, Any] = {"model": template.format(model=model_id)}
            api_base = source.get("api_base")
            if isinstance(api_base, str) and api_base:
                params["api_base"] = api_base
            api_key_env = source.get("api_key_env")
            if isinstance(api_key_env, str) and api_key_env:
                params["api_key"] = f"os.environ/{api_key_env}"
            extra_headers = source.get("extra_headers")
            if isinstance(extra_headers, dict) and extra_headers:
                params["extra_headers"] = {
                    str(key): str(value) for key, value in extra_headers.items()
                }
            deployments[f"{prefix}/{model_id}"] = {"params": params, "source": name}
    return deployments, ok_sources


def gateway_model_names() -> dict[str, dict[str, Any]]:
    payload = http_json("GET", f"{GATEWAY_URL}/model/info", token=MASTER_KEY)
    models: dict[str, dict[str, Any]] = {}
    for item in payload.get("data", []) or []:
        if not isinstance(item, dict):
            continue
        name = item.get("model_name")
        if not isinstance(name, str):
            continue
        info = item.get("model_info")
        info = info if isinstance(info, dict) else {}
        metadata = info.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        models[name] = {
            "id": info.get("id"),
            "managed_by": metadata.get("managed_by"),
        }
    return models


def create_deployment(alias: str, entry: dict[str, Any]) -> None:
    http_json(
        "POST",
        f"{GATEWAY_URL}/model/new",
        token=MASTER_KEY,
        payload={
            "model_name": alias,
            "litellm_params": entry["params"],
            "model_info": {"metadata": {"managed_by": MANAGED_BY, "sync_source": entry["source"]}},
        },
    )


def delete_deployment(model_id: Any) -> None:
    http_json("POST", f"{GATEWAY_URL}/model/delete", token=MASTER_KEY, payload={"id": model_id})


def sync_once(document: dict[str, Any]) -> dict[str, Any]:
    deployments, ok_sources = desired_deployments(document)
    existing = gateway_model_names()
    created = 0
    create_errors = 0
    for alias, entry in deployments.items():
        if alias in existing:
            continue
        try:
            create_deployment(alias, entry)
            created += 1
        except Exception as exc:
            create_errors += 1
            log(f"create {alias} failed: {type(exc).__name__}: {exc}")
    pruned = 0
    prune_errors = 0
    for name, meta in existing.items():
        if not name.startswith(f"{ALIAS_PREFIX}/"):
            continue
        if meta.get("managed_by") != MANAGED_BY:
            continue
        source_name = name.split("/")[1] if name.count("/") >= 2 else ""
        if source_name not in ok_sources or name in deployments:
            continue
        model_id = meta.get("id")
        if not model_id:
            continue
        try:
            delete_deployment(model_id)
            pruned += 1
        except Exception as exc:
            prune_errors += 1
            log(f"prune {name} failed: {type(exc).__name__}: {exc}")
    return {
        "desired": len(deployments),
        "created": created,
        "pruned": pruned,
        "create_errors": create_errors,
        "prune_errors": prune_errors,
        "sources_ok": sorted(ok_sources),
    }


def write_marker(name: str, payload: dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError as exc:
        log(f"marker write failed ({name}): {exc}")


def sleep_between_cycles() -> None:
    deadline = time.monotonic() + max(INTERVAL_MINUTES, 1) * 60
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(30.0, remaining))


def run_healthcheck() -> int:
    try:
        with open(os.path.join(STATE_DIR, "last_success"), encoding="utf-8") as handle:
            last_success = json.load(handle)
        age = time.time() - float(last_success.get("ts", 0))
    except Exception as exc:
        log(f"healthcheck: no fresh success marker: {exc}")
        return 1
    budget = max(INTERVAL_MINUTES, 1) * 120 + HEALTHCHECK_GRACE_SECONDS
    if age > budget:
        log(f"healthcheck: last success too old: {age:.0f}s > {budget:.0f}s")
        return 1
    return 0


def main() -> int:
    if "--healthcheck" in sys.argv[1:]:
        return run_healthcheck()
    log(f"starting gateway={GATEWAY_URL} interval_minutes={INTERVAL_MINUTES:g}")
    while True:
        try:
            summary = sync_once(load_document())
            log(
                "cycle done desired={desired} created={created} pruned={pruned}"
                " create_errors={create_errors} prune_errors={prune_errors}".format(**summary)
            )
            write_marker("last_success", {"ts": int(time.time()), **summary})
        except Exception as exc:
            log(f"cycle failed: {type(exc).__name__}: {exc}")
            write_marker(
                "last_error",
                {"ts": int(time.time()), "error": f"{type(exc).__name__}: {exc}"},
            )
        if MODEL_SYNC_ONCE:
            return 0
        sleep_between_cycles()


if __name__ == "__main__":
    sys.exit(main())
