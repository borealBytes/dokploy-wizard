#!/usr/bin/env python3
"""Dokploy Wizard LiteLLM model autosync sidecar.

Polls the configured model catalog sources (OpenRouter, OpenCode Zen, NVIDIA),
registers models the gateway does not expose yet through the LiteLLM admin
API, and prunes managed deployments that disappeared from their source.
Sources that declare a rankings_url (OpenRouter) register the leaderboard's
top paid (standard-variant) models followed by the top ":free" variants
instead of an arbitrary catalog slice.
Sources that declare "probe": true (NVIDIA) are liveness-checked with a
1-token chat completion before the per-source budget is applied: models the
provider 404s are dropped, everything else is kept, verdicts are cached under
the state dir for probe_ttl_days, and dead models are re-probed every cycle.

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
  MODEL_SYNC_CONSUMER_KEYS_FILE
                              JSON map of consumer name -> virtual key token
                              (written by the wizard into the state volume).
                              When present each cycle also refreshes those
                              keys' model_access lists so /v1/models exposes
                              the current sync/* set explicitly. Touching the
                              file triggers an early cycle.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib import request
from urllib.parse import quote

SOURCES_FILE = os.environ.get("MODEL_SYNC_SOURCES_FILE", "/app/sources.json")
STATE_DIR = os.environ.get("MODEL_SYNC_STATE_DIR", "/state")
GATEWAY_URL = os.environ.get("LITELLM_GATEWAY_URL", "http://litellm:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
INTERVAL_MINUTES = float(os.environ.get("MODEL_SYNC_INTERVAL_MINUTES", "360"))
MODEL_SYNC_ONCE = os.environ.get("MODEL_SYNC_ONCE", "0") == "1"
HEALTHCHECK_GRACE_SECONDS = 900
MANAGED_BY = "dokploy-wizard-model-sync"
ALIAS_PREFIX = "sync"
SYNC_WILDCARD = "sync/*"
CONSUMER_KEYS_FILE = os.environ.get("MODEL_SYNC_CONSUMER_KEYS_FILE", "/state/consumer-keys.json")
CYCLE_POLL_SECONDS = 30.0
USER_AGENT = "dokploy-wizard-model-sync/1"
REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_MODELS_PER_PROVIDER = 50
OPENROUTER_RANKINGS_METRIC_FIELD = "rankingMetricValue"
DEFAULT_MAX_RANKED_PAID = 20
DEFAULT_MAX_RANKED_FREE = 10
DEFAULT_PROBE_TTL_DAYS = 7
DEFAULT_PROBE_TIMEOUT_SECONDS = 8.0
DEFAULT_PROBE_PARALLELISM = 6


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[model-sync] {stamp} {message}", flush=True)


def http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, method=method, data=body)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def load_document() -> dict[str, Any]:
    with open(SOURCES_FILE, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"sources document must be an object: {SOURCES_FILE}")
    return document


def _chat_catalog_by_variant(catalog: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Index a chat-capable catalog by canonical slug for leaderboard joins.

    Returns (paid_by_slug, free_by_slug): paid maps to the plain base id
    (no variant suffix), free maps to the ":free" variant id. Entries with
    other variants (":batch", ":extended", ...) are not joinable.
    """

    paid_by_slug: dict[str, str] = {}
    free_by_slug: dict[str, str] = {}
    for item in catalog.get("data", []) or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if not model_id or model_id.endswith("/*"):
            continue
        architecture = item.get("architecture") or {}
        outputs = architecture.get("output_modalities") or []
        if outputs and "text" not in outputs:
            continue
        slug = str(item.get("canonical_slug") or model_id).strip()
        if ":" not in model_id:
            paid_by_slug.setdefault(slug, model_id)
        elif model_id.endswith(":free"):
            free_by_slug.setdefault(slug, model_id)
    return paid_by_slug, free_by_slug


def _ranked_catalog_model_ids(source: dict[str, Any], catalog: dict[str, Any]) -> list[str] | None:
    """Order OpenRouter-style catalogs by the public weekly leaderboard.

    Returns the top standard-variant ("paid") callable ids followed by the
    top ":free"-variant callable ids, each block ordered by weekly tokens
    processed. Ranked entries with no joinable chat catalog id are skipped.
    Returns None when no usable leaderboard data exists so the caller can
    fall back to plain catalog order.
    """

    rankings_url = source.get("rankings_url")
    if not (isinstance(rankings_url, str) and rankings_url):
        return None
    paid_by_slug, free_by_slug = _chat_catalog_by_variant(catalog)
    try:
        payload = http_json("GET", rankings_url)
    except Exception as exc:
        log(
            f"source {source.get('name') or '?'} rankings fetch failed: "
            f"{type(exc).__name__}: {exc}; using catalog order"
        )
        return None
    paid_totals: dict[str, float] = {}
    free_totals: dict[str, float] = {}
    for row in payload.get("data", []) or []:
        if not isinstance(row, dict):
            continue
        slug = row.get("model_permaslug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = slug.strip()
        try:
            value = float(row.get(OPENROUTER_RANKINGS_METRIC_FIELD) or 0)
        except (TypeError, ValueError):
            value = 0.0
        variant = str(row.get("variant") or "").strip().lower()
        if variant == "free":
            free_totals[slug] = free_totals.get(slug, 0.0) + value
        elif variant == "standard" or not variant:
            paid_totals[slug] = paid_totals.get(slug, 0.0) + value
    if not paid_totals and not free_totals:
        log(f"source {source.get('name') or '?'} rankings empty; using catalog order")
        return None

    def _top(totals: dict[str, float], index: dict[str, str], limit: int) -> list[str]:
        picked: list[str] = []
        for slug in sorted(totals, key=lambda name: -totals[name]):
            if totals[slug] <= 0:
                continue
            model_id = index.get(slug)
            if model_id is None:
                continue
            if model_id in picked:
                continue
            picked.append(model_id)
            if len(picked) >= limit:
                break
        return picked

    def _limit(source_key: str, default_limit: int) -> int:
        raw = source.get(source_key)
        if isinstance(raw, (int, float)) and int(raw) > 0:
            return int(raw)
        return default_limit

    ranked = _top(paid_totals, paid_by_slug, _limit("max_ranked_paid", DEFAULT_MAX_RANKED_PAID))
    free_ids = _top(free_totals, free_by_slug, _limit("max_ranked_free", DEFAULT_MAX_RANKED_FREE))
    return ranked + [model_id for model_id in free_ids if model_id not in ranked]


def _probe_cache_path(source_name: str) -> str:
    return os.path.join(STATE_DIR, f"probe-cache-{source_name}.json")


def _load_probe_cache(source_name: str, ttl_seconds: float) -> dict[str, str]:
    """Return cached model_id -> verdict verdicts while the cache is fresh."""

    try:
        with open(_probe_cache_path(source_name), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    checked_at = raw.get("checked_at")
    results = raw.get("results")
    if not isinstance(checked_at, (int, float)) or not isinstance(results, dict):
        return {}
    if time.time() - float(checked_at) > ttl_seconds:
        return {}
    return {
        str(model_id): str(verdict)
        for model_id, verdict in results.items()
        if verdict in ("alive", "dead", "unknown")
    }


def _save_probe_cache(source_name: str, results: dict[str, str]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_probe_cache_path(source_name), "w", encoding="utf-8") as handle:
            json.dump({"checked_at": time.time(), "results": results}, handle)
    except OSError as exc:
        log(f"source {source_name} probe cache save failed: {type(exc).__name__}: {exc}")


def _probe_model_status(
    source: dict[str, Any], token: str, model_id: str, timeout: float
) -> str:
    """Chat-probe one model: alive on 2xx, dead on a clean 404, else unknown.

    The probe is a 1-token chat completion, which also filters embedding,
    transcription, and utility models (they reject chat requests) without
    counting as dead so an inconclusive answer never prunes a working model.
    """

    api_base = str(source.get("api_base", "")).rstrip("/")
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        http_json(
            "POST",
            f"{api_base}/chat/completions",
            token=token or None,
            payload=payload,
            timeout=timeout,
        )
        return "alive"
    except Exception as exc:  # noqa: BLE001 - any transport failure must not sink the cycle
        if getattr(exc, "code", None) == 404:
            return "dead"
        return "unknown"


def _free_model_ids(source: dict[str, Any]) -> set[str] | None:
    """Free-tier model ids from the models.dev registry, or None on failure.

    NVIDIA's servable /models list mixes free and paid endpoints, and a paid
    chat hit spends credits. models.dev (the registry OpenCode itself ships)
    carries per-model pricing keyed in the same org/model form, so the sync
    keeps only zero-cost endpoints before anything gets probed.
    """

    url = source.get("free_catalog_url")
    if not (isinstance(url, str) and url):
        return None
    provider = str(source.get("free_catalog_provider") or "nvidia")
    try:
        payload = http_json("GET", url)
    except Exception as exc:
        log(f"source {provider} free-catalog fetch failed: {type(exc).__name__}: {exc}; keeping full list")
        return None
    models = ((payload.get(provider) or {}).get("models") or {}) if isinstance(payload, dict) else {}
    free: set[str] = set()
    for model_id, meta in models.items():
        if not isinstance(meta, dict):
            continue
        cost = meta.get("cost") or {}
        try:
            paid = any(float(cost.get(field) or 0) > 0 for field in ("input", "output"))
        except (TypeError, ValueError):
            paid = True
        if not paid:
            free.add(str(model_id))
    return free or None


def probe_source_models(source: dict[str, Any], token: str, model_ids: list[str]) -> list[str]:
    """Drop models the provider 404s on chat completion, keep everything else."""

    name = str(source.get("name") or "?")
    api_base = source.get("api_base")
    if not (isinstance(api_base, str) and api_base) or not model_ids:
        return model_ids
    try:
        ttl_days = float(source.get("probe_ttl_days") or DEFAULT_PROBE_TTL_DAYS)
    except (TypeError, ValueError):
        ttl_days = DEFAULT_PROBE_TTL_DAYS
    try:
        parallelism = int(source.get("probe_parallelism") or DEFAULT_PROBE_PARALLELISM)
    except (TypeError, ValueError):
        parallelism = DEFAULT_PROBE_PARALLELISM
    results = _load_probe_cache(name, ttl_days * 86400.0)
    # Known-alive models ride the cache; dead and inconclusive ones are
    # re-probed every cycle so recovered models come back without a rebuild.
    to_probe = [model_id for model_id in model_ids if results.get(model_id) != "alive"]
    if to_probe:
        timeout = float(source.get("probe_timeout_seconds") or DEFAULT_PROBE_TIMEOUT_SECONDS)
        workers = max(1, min(max(1, parallelism), len(to_probe)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(
                pool.map(
                    lambda model_id: _probe_model_status(source, token, model_id, timeout),
                    to_probe,
                )
            )
        for model_id, verdict in zip(to_probe, statuses):
            results[model_id] = verdict
        alive_now = sum(1 for verdict in statuses if verdict == "alive")
        dead_now = sum(1 for verdict in statuses if verdict == "dead")
        log(
            f"source {name} liveness probe: checked {len(to_probe)} "
            f"(alive={alive_now} dead={dead_now} "
            f"inconclusive={len(to_probe) - alive_now - dead_now}, "
            f"cached-alive reused={len(model_ids) - len(to_probe)})"
        )
    dead = [model_id for model_id in model_ids if results.get(model_id) == "dead"]
    alive_total = sum(1 for model_id in model_ids if results.get(model_id) == "alive")
    if dead and alive_total == 0:
        # Everything probed either died or stayed inconclusive: this is the
        # signature of an upstream brownout (or auth breakage) returning 404
        # for healthy models too, so keep the full list rather than emptying
        # the provider from the picker.
        log(
            f"source {name} probe brownout: 0 alive with {len(dead)} dead hits; "
            "keeping full catalog order this cycle"
        )
        return model_ids
    _save_probe_cache(name, results)
    if dead:
        log(
            f"source {name} pruning {len(dead)} dead models from the sync set: "
            f"{', '.join(dead[:10])}"
        )
    return [model_id for model_id in model_ids if results.get(model_id) != "dead"]


def source_model_ids(source: dict[str, Any], max_models: int) -> list[str]:
    api_key_env = source.get("api_key_env")
    token = os.environ.get(api_key_env, "") if isinstance(api_key_env, str) else ""
    payload = http_json("GET", source["list_url"], token=token or None)
    limit = max_models
    source_max = source.get("max_models")
    if isinstance(source_max, (int, float)) and int(source_max) > 0:
        # A source's own budget fully overrides the global per-provider cap so
        # catalogs the operator wants in full (e.g. OpenCode Go) are not cut.
        limit = int(source_max)
    ranked = _ranked_catalog_model_ids(source, payload)
    if ranked is not None:
        return ranked[:limit]
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
    if source.get("free_only"):
        free_ids = _free_model_ids(source)
        if free_ids is not None:
            kept = [model_id for model_id in model_ids if model_id in free_ids]
            if kept:
                log(
                    f"source {source.get('name') or '?'} free-tier filter: "
                    f"{len(model_ids)} -> {len(kept)} (registry lists {len(free_ids)} free)"
                )
                model_ids = kept
            else:
                log(
                    f"source {source.get('name') or '?'} free-tier filter matched nothing "
                    f"(registry drift?); keeping full catalog order"
                )
    if source.get("probe"):
        # Probe the full catalog first, then apply the budget: "top N" must
        # mean the first N that actually answer, not the first N listed.
        model_ids = probe_source_models(source, token, model_ids)
    return model_ids[:limit]


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
                # The admin API does not resolve os.environ/ references for
                # DB-stored deployments (unlike config-file models), so the
                # sidecar resolves the key from its own environment and sends
                # the literal value; the reference form is only a fallback.
                resolved_key = os.environ.get(api_key_env)
                params["api_key"] = (
                    resolved_key if resolved_key else f"os.environ/{api_key_env}"
                )
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


def load_consumer_keys() -> dict[str, str]:
    """Read the wizard-pushed consumer key tokens ({consumer: token}).

    Missing or malformed files simply disable key reconciliation; the wizard
    re-pushes the file after every key rotation and an early cycle follows.
    """

    try:
        with open(CONSUMER_KEYS_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log(f"consumer keys unreadable: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(consumer): token.strip()
        for consumer, token in raw.items()
        if isinstance(token, str) and token.strip()
    }


def _key_models_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    info = payload.get("info")
    for container in (payload, info if isinstance(info, dict) else {}):
        models = container.get("models")
        if isinstance(models, list):
            return [str(model) for model in models if isinstance(model, str) and model.strip()]
    return []


def reconcile_consumer_key(consumer: str, token: str, desired_aliases: list[str]) -> bool:
    """Merge the current sync/* alias set into one consumer key's allowlist.

    LiteLLM does not expand the sync/* wildcard on /v1/models for keys, so
    consumers that seed their model pickers from that endpoint only see the
    synced set when it is listed explicitly. Non-sync entries and the wildcard
    itself are preserved; stale sync/* names from previous rotations are
    dropped. Returns True when the key was updated.
    """

    info_url = f"{GATEWAY_URL}/key/info?key={quote(token, safe='')}"
    payload = http_json("GET", info_url, token=MASTER_KEY)
    models = _key_models_payload(payload)
    if not models:
        log(f"key reconcile {consumer}: no allowlist visible via /key/info; leaving key alone")
        return False
    desired = set(desired_aliases)
    retained = [
        model
        for model in models
        if not model.startswith(f"{ALIAS_PREFIX}/") or model == SYNC_WILDCARD or model in desired
    ]
    merged = retained + [alias for alias in desired_aliases if alias not in retained]
    if merged == models:
        return False
    # Pinned LiteLLM reads the allowlist off `models` in /key/update and
    # silently ignores `model_access`; send both for version tolerance.
    http_json(
        "POST",
        f"{GATEWAY_URL}/key/update",
        token=MASTER_KEY,
        payload={"key": token, "models": merged, "model_access": merged},
    )
    return True


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
    key_reconciled = 0
    key_errors = 0
    consumer_keys = load_consumer_keys()
    if consumer_keys and deployments:
        desired_aliases = sorted(deployments)
        for consumer, token in sorted(consumer_keys.items()):
            try:
                if reconcile_consumer_key(consumer, token, desired_aliases):
                    key_reconciled += 1
            except Exception as exc:
                key_errors += 1
                log(f"key reconcile {consumer} failed: {type(exc).__name__}: {exc}")
    return {
        "desired": len(deployments),
        "created": created,
        "pruned": pruned,
        "create_errors": create_errors,
        "prune_errors": prune_errors,
        "key_reconciled": key_reconciled,
        "key_errors": key_errors,
        "sources_ok": sorted(ok_sources),
    }


def write_marker(name: str, payload: dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError as exc:
        log(f"marker write failed ({name}): {exc}")


def consumer_keys_mtime() -> float:
    try:
        return os.path.getmtime(CONSUMER_KEYS_FILE)
    except OSError:
        return 0.0


def wait_for_next_cycle(trigger_baseline: float) -> float:
    """Sleep between cycles, waking early when the wizard pushes new tokens.

    Returns the refreshed trigger baseline (interval expiry) or a negative
    sentinel when the consumer-keys file changed and a cycle should run now.
    """

    deadline = time.monotonic() + max(INTERVAL_MINUTES, 1) * 60
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return trigger_baseline
        time.sleep(min(CYCLE_POLL_SECONDS, remaining))
        current = consumer_keys_mtime()
        if current > trigger_baseline:
            return -1.0


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
    trigger_baseline = consumer_keys_mtime()
    while True:
        try:
            summary = sync_once(load_document())
            log(
                "cycle done desired={desired} created={created} pruned={pruned}"
                " create_errors={create_errors} prune_errors={prune_errors}"
                " key_reconciled={key_reconciled} key_errors={key_errors}".format(**summary)
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
        refreshed = wait_for_next_cycle(trigger_baseline)
        if refreshed < 0:
            log("consumer keys file updated; running early cycle")
            trigger_baseline = consumer_keys_mtime()
        else:
            trigger_baseline = refreshed


if __name__ == "__main__":
    sys.exit(main())
