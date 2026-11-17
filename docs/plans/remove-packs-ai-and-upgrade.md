# dokploy-wizard — Consolidated PR: pack removals + built-in AI wiring + `upgrade` command

Plan of record, captured 2026-11-17. Branch `feature/removal-ai-upgrade` off `origin/main`
at `103e397` (PR #13 merged, Hermes Desktop webapp complete). Follows
`docs/plans/hermes-desktop-webapp-and-follow-ons.md` — this document merges its PR B,
PR C, and the new built-in AI work (PR D) into **one PR** with four contiguous commit
groups, ordered so each group lands on the final shape of the lines the next group edits.

Ground rules carry over unchanged from the PR A doc: nothing on `main` directly, zero
local Docker, all install/modify/upgrade tests on the remote VPS, surgical README edits,
no push/PR without being asked.

## Test box (Phase 0 verified 2026-11-17)

| Fact | Value |
|---|---|
| Host | `157.173.124.231` (`vmi3712111`), Contabo VPS, Ubuntu, Docker 29.9.2 |
| Domain | `showmeseewhy.com` — zone `851f00ec6d0c3a567a9b7197c9b04ad4`, account `4dee5b78550a3c0654b9604106abb591` |
| Env file | `.install-min-testing.env` (physically in `feature/hermes-desktop-webapp/`; symlink at worktree root — **move the file somewhere durable before pruning that worktree**) |
| Env shape | `PACKS=seaweedfs,coder,docuseal,nextcloud`, bare `AI_DEFAULT_MODEL=deepseek-v4.1-flash`, `AI_DEFAULT_MODEL_PATTERN=deepseek-v4*flash*`, `LITELLM_ADMIN_SUBDOMAIN=litellm`, OTP email set, Tailscale off |
| Logins | SSH root password works; Dokploy `sign-in/email` 200 + session OK (user created 2026-11-02); Cloudflare token active, zone readable |
| Public | `dokploy.` 200, `coder.` 200, `litellm.` behind Access OTP, root apex no HTTPS answer |

**Why a blank slate, not `modify`:** the ownership ledger lives in the state directory
that was collected in the GitHub Codespaces workspace, not on this machine. The only
local `.dokploy-wizard-state` is a stale `example.com` fixture. The ledger is the
uninstall/modify authority, so the box is torn down (Contabo reinstall image) and
reinstalled fresh, which also sidesteps the fingerprint-drift no-op bug (PR A doc A3b:
code-only shared-core changes have no rollout path on existing boxes) and the
code-only-removal ledger traps (A3 table). The box ran no removed packs, so nothing of
ours is stranded beyond Dokploy itself.

## Group 1 — Removals (PR C): OpenClaw, Nexa, Telly, My Farm Advisor, SurfSense

Code-only removal. Existing boxes are reinstalled, never upgraded into the new tree.

| Removed | Detail |
|---|---|
| `dokploy/openclaw.py`, `packs/openclaw/` (incl. Nexa + Telly — they live here) | ~4 files, 4.4k LOC + Nexa src, Dockerfiles, fixtures, 12 Nexa tests (~8.2k LOC) |
| `dokploy/surfsense.py`, `surfsense_backend.py`, `packs/surfsense/` | ~7 files, 4.1k LOC |
| My Farm Advisor | variant switch inside the OpenClaw backend; farm compose template + 2 farm `.env.example` |
| Tests | OpenClaw unit + integration (~5k LOC); rewrite ~350–400 functions across ~50 files (`tests/test_cli.py` alone ≈100) |
| Templates/docs | openclaw compose template, `docs/incidents/openclaw-trusted-proxy-scopes.md` |

Total ~42–46 files / ~21.8k LOC deleted; ~85 files edited in place.

### In-place rewrites and re-keys

| Site | Action |
|---|---|
| `state/models.py` `LITELLM_CONSUMER_VIRTUAL_KEY_NAMES` / `_PREFIXES` | drop `openclaw`, `my-farm-advisor`, `surfsense` |
| Nextcloud advisor plumbing | drop `_ensure_nexa_service_account`, `/Nexa Claw` mount, `_ensure_openclaw_rescan_schedule` + `NEXTCLOUD_OPENCLAW_RESCAN_*`, `DOKPLOY_WIZARD_OPENCLAW_*` env writes, `NextcloudOpenClawWorkspaceContract`. Talk/OnlyOffice stay |
| Shared Postgres | keep `pgvector/pgvector:pg16`; drop per-DB `SUPERUSER` grant, `CREATE EXTENSION vector`, `wal_level=logical` + slot args (SurfSense-Zero-only: `_allocation_requires_surfsense_postgres_capabilities`, `shared_core.py:243,2095`) |
| `CLOUDFLARE_ACCESS_OTP_EMAILS` gating (`state/env.py:502-510`) | re-key onto `litellm-admin`/`coder` — survives with no pack needing it today. `_access_target_hostnames` (`networking/planner.py:569-589`) loses openclaw/farm entries |
| `preflight.derive_required_profile()` | `FULL_PACK_SET_PROFILE` trigger `openclaw or my-farm-advisor` → heavy survivors (`nextcloud`, `matrix`, `coder`) |
| `packs/prompts.py` | delete farm-only prompts/keys after confirming no survivor reads them |
| `qa_harness.py:238`, `remote.py:609` | hard indices on removed consumers → rewrite |
| SurfSense lifecycle | unwrap 2 `PHASE_ORDER` entries, 1 drift branch, `SurfSenseGeneratedSecrets` + state file, 9 `cli.py` sites |
| Fixtures | `invalid-pack-combo.env` needs a new offending pack; `tests/helpers/root_install_env.py` neutralized |
| Tests | 5 farm-only tests inside OpenClaw files get relocated, not deleted |
| `uninstall/planner.py::_RULES` | **keep** the `openclaw_service` / `my_farm_advisor_service` / `surfsense_service` delete rules (cheap insurance so an old box can still uninstall) |
| Docs | README: delete `### OpenClaw`, `### Nexa`, `### Telly`, `### Nexa credential mediation`, `### My Farm Advisor`, `### SurfSense`, `## My Farm Advisor operator reference` (~250/948 lines); edit ~20 sections in place. AGENTS.md pack list + focused-modules block. Leave `.sisyphus/evidence/**` alone |

Accepted stranding on boxes that ran these packs (documented, no cleanup code):
openclaw/farm/surfsense compose apps, 4 untracked data volumes, per-pack Postgres
DB/role/grants, three virtual keys, Cloudflare DNS/tunnel/Access entries, Nextcloud
`nexa` account + `/Nexa Claw` mount + rescan schedule, `local/dokploy-wizard-nexa-*`
images. Old `desired-state.json` files with required-dropped-fields are unreadable by
`inspect-state` — accepted under code-only retirement.

**Gate:** `uv run --all-extras pytest -q && ruff check . && mypy .` + a grep-gate test
asserting zero surviving `openclaw|nexa|telly|my.?farm|surfsense` references in `src/`.

## Group 2 — Built-in AI wiring (PR D)

Today Dokploy's AI shows only the default model — by construction, in three sites:
`shared_core.py:1722` (`visible_aliases_by_consumer["dokploy-ai"] = default_aliases[:1]`),
`shared_core.py:1648` (dokploy-ai is the only consumer excluded from the `sync/*`
wildcard append), and `shared_core.py:597` (dokploy-ai excluded from the consumer-key
push that lets the sidecar refresh allowlists). Nextcloud has **no** LiteLLM wiring at
all (six consumers today, none of them nextcloud). DocuSeal: no built-in AI. Moodle's
AI-provider plugin is a possible future consumer; not in this PR.

| Site | Action |
|---|---|
| `state/models.py` | add `nextcloud` consumer + `sk-litellm-nextcloud` prefix |
| `shared_core.py` | render `LITELLM_VIRTUAL_KEY_NEXTCLOUD` into the nextcloud service (mirror how `openclaw` consumed its key); nextcloud allowlist = shared aliases + `sync/*` |
| Nextcloud reconciler | idempotent `occ app:install integration_openai` + `config:app:set` base URL (internal `http://<litellm>:4000/v1`) + key + default model; upsert semantics |
| Dokploy | remove the `[:1]` slice (`shared_core.py:1709`), the `sync/*` wildcard-append exclusion (`:1637`), and the sidecar consumer-key push exclusion (`:590`); **D1**: seed one Dokploy AI provider row per configured alias, reconciled by wizard-owned name at install/modify |
| Tests | allowlist-shape tests for both keys; provider-row reconcile tests; "every consumer key allows the default" test extended |

Follow-on (not in this PR): D2 — the sync sidecar reconciles Dokploy provider rows
live from `/v1/models`.

### D1 verified schema facts (upstream `Dokploy/dokploy` main, 2026-11-17)

- `ai.delete` exists: mutation input `{aiId}`, permission `ai delete`, returns `true`
  (`apps/dokploy/server/api/routers/ai.ts`).
- `ai.create` input is `apiCreateAi` = exactly `{name, apiUrl, apiKey, model, isEnabled}`,
  all required (`packages/server/src/db/schema/ai.ts`); the `ai` table has one `model`
  text column — multi-model rows are impossible, confirming D1.
- `ai.getAll` strips `apiKey` from every row, so reconcile comparisons must key on
  (wizard-name, apiUrl, model, isEnabled) and re-send the key on `ai.update`;
  `apiUpdateAi` is partial + `aiId`.
- Rows are organization-scoped; `name` is free text (min 1 char).
- Row naming scheme: `Dokploy Wizard LiteLLM · <alias>` per alias; the exact un-suffixed
  base name counts as wizard-owned (legacy single row) and gets replaced by the
  default-alias row. Never touch rows the wizard didn't name.
- Desired alias set = configured catalog aliases (`_configured_litellm_aliases` from the
  same rendered LiteLLM config) plus the default alias; verification asserts set equality
  and runs `ai.testConnection` for the default-alias row only.

### Group 2 implementation notes (landed 2026-11-17)

Deviations from the planning table, with reasons:

- The `nextcloud` virtual key is not rendered into the Nextcloud compose as an env var.
  The key is written into Nextcloud's app config via occ, so it never appears in the
  Dokploy compose file or env spec surface; the value comes from
  `load_litellm_generated_keys(state_dir)` (`_repair_litellm_generated_keys` backfills
  the slot for pre-Group-2 state).
- App target: the appstore resolver picks `integration_openai` **v3.9.1** for the
  deployed `nextcloud:apache` major (spec `>=30.0.0 <34.0.0`, first matching release in
  feed order). v3.9.x is the single-service redesign: flat app-config keys `url`,
  `api_key` (stored with sensitive flag, but occ-written plaintext reads back verbatim —
  the server only decrypts values carrying its encryption prefix), and `service_name`.
  The model list is fetched live by the app from `{url}/models`; there is no
  `availableModels` config key. v4.x/v5.x (NC33+/NC35) move to a services-based
  `SERVICES_CONFIG_KEY` JSON shape — a future major-bump follow-on, same slot as D2.
- `_ensure_ai_integration_openai(container, integration)` (in `dokploy/nextcloud.py`)
  runs on both `ensure_application_ready` branches after OnlyOffice wiring: enable with
  release fallback (same `_enable_app_with_release_fallback` pattern as onlyoffice/
  spreed), set the three keys, then seed the app's cached model list (`models` app
  config, OpenAI `{"data":[{"id","object"}],"object":"list"}` shape) from the union of
  the static `nextcloud` consumer allowlist and a bounded live probe of
  `http://127.0.0.1:4000/v1/models` authenticated with the consumer key. Probe failure
  is soft (the app refetches when an admin opens its settings); enable failure raises.
- Dokploy row reconcile (D1, as planned): desired set = `dokploy-ai` consumer allowlist
  (shared aliases + default-chat alias) minus the `sync/*` literal, widened at apply
  time by the live `/v1/models` probe (docker exec, master key) when the wildcard is
  present; only `sync/`-prefixed exposed aliases join the row set. Rows for wizard-named
  aliases outside the desired set are deleted; duplicates per alias collapse to the
  first row. Hidden (blank) visible keys are treated as current, not drifted.
- Verification never probes the gateway: requires every static-set alias to have a
  correctly named row, tolerates extra `sync/*` rows, reports drift detail
  `{missing_aliases, unexpected_aliases, duplicate_aliases}` with reason
  `provider_state_drift`, and runs `ai.testConnection` against the default-alias row.
- Provider row seeding now runs **after** `_reconcile_model_sync_visibility` so rows
  seed from the post-first-cycle gateway view.

## Group 3 — `upgrade` command (PR B, scoped to O1+O2)

Command name: `upgrade` (semantics include major-version gates; "update" undersells it).

| Phase | Action | Gate |
|---|---|---|
| `upgrade --check` | drift report per service: running vs newest registry digest (never trust `:latest` labels), pins (`LITELLM_IMAGE_TAG`, Moodle PHP, Zero, SearXNG), workspace/CLI versions | read-only |
| 1–2 | plan from ledger → snapshot (state checkpoint + `pg_dump` shared core; `hermes backup` where present) | snapshot required before apply |
| 3 | apply per service in dependency order (core → LiteLLM → packs → Coder): force re-pull + recreate floating services; `--litellm <tag>` bumps the pin; **majors refused without `--allow-major`** (Postgres needs dump/restore; Nextcloud needs `occ upgrade` + maintenance mode; LiteLLM bumps operator-approved only) | health check per service; previous tag from ledger = rollback |
| 4 | Coder template reseed (content-hash machinery already does the right thing) | existing |
| 5 | workspaces: report which are behind + agent versions; restart/recreate only on explicit flag | recreate is the only destructive action |
| 6 | report + ledger write | — |

Deferred follow-ons: O3 `versions.json` release manifest, O5 nightly throwaway-VPS
proof, D2, DocuSeal/Moodle AI.

### Group 3 implementation notes (landed 2026-11-17)

Deviations from the planning table, with reasons:

- Scope landed as O1 (drift report) + a **LiteLLM-only** apply. The per-service forced
  re-pull ladder (packs, Postgres majors, Nextcloud `occ upgrade` chain) stays deferred:
  every pack except LiteLLM floats on `:latest`-class tags where the honest answer is
  "rerun modify re-pulls", and the Postgres/Nextcloud majors need restore/maintenance
  machinery that deserves its own gate. `--litellm <tag>` is the one component the
  wizard pins, so it is the one component with a real upgrade story.
- Drift truth: `docker service inspect` refs (swarm services are what the wizard
  creates) parsed into registry/repository/tag/digest, compared against anonymous
  `Docker-Content-Digest` manifest fetches with bearer-challenge token exchange and
  `Link`-rel=next tag pagination. Classes: `current`, `digest_stale`, `tag_only_ref`
  (running ref carries no digest — floating tag, digest comparison impossible).
- Pin resolution is exactly one chokepoint: `apply_litellm_image_pin_to_env` rewrites
  `LITELLM_IMAGE`/`LITELLM_IMAGE_TAG` in `_build_shared_core_backend`, so install,
  modify, and upgrade passes all render the state pin without teaching call sites
  about the document. Precedence: pin > env > `DEFAULT_LITELLM_IMAGE(_TAG)`.
- Apply sequence: typed confirmation (the word `upgrade`; `y`/`yes`/`ok`/`confirm`
  rejected; non-interactive requires `--confirm-file`) → snapshot → pin write →
  clear the shared-core compose artifact hash (key = `network_name`, e.g.
  `<stack>-shared`) → reuse `run_modify_flow`. Clearing only that hash means exactly
  one forced shared-core redeploy; every pack stays noop-skippable.
- Snapshot: state-dir copytree excluding `snapshots/`, plus gzipped `pg_dumpall` via
  `docker exec` (`-U postgres`, password = the deterministic
  `_shared_core_generated_secret("<pg-service>-root-password")`), plus best-effort
  `hermes backup` (note-only; absence or failure never blocks apply).
- Major gate: `--allow-major` covers bump **and** downgrade, so the rollback command
  (`upgrade --litellm <previous-tag>`) is subject to the same discipline as the bump.
- Workspaces are report-only (`coder workspaces ls --output json` via exec); no
  restart/recreate flags in this cut.
- Remote helper gained an `upgrade` passthrough mirroring `modify` (bundle upload +
  `_build_upgrade_command`); apply requires `--confirm-file`, which is uploaded with
  the rest of the run artifacts.

## Group 4 — Default workspace name fix (A3b carryover)

The date-stamped default workspace name makes every post-midnight `modify` create a
second full Hermes stack (observed 2026-10-31 → 2026-11-01 on the PR A box, and it
would poison our own repeated-modify tests). Fix: adopt-or-name at first create — a
stable name, adopt an existing default-template workspace rather than minting dated
duplicates; prune wizard-created leftovers.

### Group 4 implementation notes (landed 2026-11-17)

- `_default_workspace_name` is now date-free: `<root-token>-workspace` (root token =
  hostname minus the `coder.` prefix, non-alphanumerics stripped, clamped to the 22
  characters the stable suffix leaves inside Coder's 32-char name limit).
- `_ensure_default_workspace` returns a `DefaultWorkspaceOutcome`
  (`exists | created | adopted` + `adopted_from` + `pruned`). Legacy detection
  (`_legacy_default_workspace_names`) mirrors the **old** truncation exactly: the
  dated suffix consumed 21 of 32 characters, so legacy roots were clamped to 11 —
  e.g. `coder.yourwebsite.com` adopted as `yourwebsite-workspace-YYYY-MM-DD` but
  renames to `yourwebsitecom-workspace`.
- Adoption: newest legacy match is renamed via `coder update --name --yes` (no third
  stack minted). Pruning: remaining legacy matches are deleted via `coder delete
  --yes` only after the stable workspace exists.
- Blast-radius bound: only legacy-shaped names (`<legacy-root>-workspace-<ISO date>`)
  are candidates, and only when `coder show` confirms the workspace sits on the
  wizard-seeded default template. A foreign template on a legacy-shaped name is the
  user's: left alone. An unresolvable template (CLI shape drift) is treated as
  wizard-owned so the dedup can never silently stop converging.
- Verification tolerates a legacy match as "default workspace present" (adoption
  happens on the same pass; a hard miss would fight the in-flight rename).
- Notes: `Renamed legacy default Coder workspace '<old>' to '<stable>'.` and
  `Pruned duplicate wizard-created Coder workspace '<name>'.`; the second
  `ensure_application_ready` pass is a true noop (flow-level idempotency test).

## Phase 6 proof log

- Attempt 1 (commit `7b106d4`+ tree): wrapper invoked with system `python3` →
  paramiko missing → instant fail, box untouched. Documented in AGENTS.md; use
  `uv run ./bin/dokploy-wizard-remote …`.
- Attempt 2: install reached Dokploy bootstrap, then `GET /api/project.all` →
  **HTTP 429 `{"message":"Rate limit exceeded"}`** in
  `_qualify_dokploy_mutation_auth`. Root cause: self-hosted Dokploy forwards
  `user.createApiKey` input verbatim to better-auth's api-key plugin, whose
  defaults are `rateLimitEnabled: true, timeWindow: 24h, maxRequests: 10` — the
  wizard's own API key carried a 10-calls-per-day cap. Dokploy's UI creates keys
  with `rateLimitEnabled: false`; the wizard now does the same (commit `79b5433`),
  plus the API client honors `Retry-After` on 429 with a bounded backoff.
  Remote was reset (`docker service rm dokploy dokploy-postgres`,
  `docker rm -f dokploy-traefik`, `rm -rf /root/dokploy-wizard`) so a fresh
  unthrottled key is created.

## Verification matrix

| Scenario | Command | Proves |
|---|---|---|
| Local gates, every commit | `uv run --all-extras pytest -q && ruff check . && mypy .` | AGENTS.md gate |
| Fresh install (after box wipe) | `./bin/dokploy-wizard-remote proof --host 157.173.124.231 --password … --env-file .install-min-testing.env` | slim tree installs; Nextcloud AI picker lists the full synced set; Dokploy Ask-AI lists every alias; Hermes Desktop tile regression (browser/CDP check, not curl — PR A doc A3b CSP lesson) |
| Modify ×2 | `…-remote modify …` twice | idempotent, one workspace (proves Group 4) |
| Drift report | `…-remote upgrade --check …` | digest-resolved table; closes the "newest `litellm:*-stable` + CVE list" backlog item |
| Upgrade apply | `…-remote upgrade … --litellm <approved tag>` + one forced failure | real redeploy on a pinned component; ledger rollback restores previous tag |
| Survivor regression | browser + CLI pass | coder/Hermes tile, community WebUI, nextcloud, moodle, docuseal, seaweedfs, Access in front of litellm |

Remote runs: timeout ≥ 3,600,000 ms or background (AGENTS.md).

## Operator asks

1. Reinstall-image the Contabo box (blank slate) before Phase 6; reuse the current root
   password or update `.install-min-testing.env` — only that file needs the change.
2. Approve the LiteLLM target tag used in the upgrade-apply test (drift report proposes it).
3. sglang-over-Tailscale values (`LITELLM_LOCAL_BASE_URL`/`_MODEL`) whenever convenient —
   optional for this PR, closes the A7 backlog item.
4. Decide later when the old worktrees are pruned: `.install-min-testing.env` currently
   lives inside `feature/hermes-desktop-webapp/`.

## Decision log

| Question | Answer |
|---|---|
| PR shape | one PR, four contiguous groups (user): easier conflict story, one proof run, bisectable |
| Group order | removals → AI wiring → upgrade → workspace-name fix (each lands on the smaller/final tree) |
| Dokploy picker | D1 (wizard-seeded rows per alias) now; D2 (sidecar-reconciled) as follow-on |
| Upgrade scope | O1+O2; `versions.json` and nightly proof deferred |
| Command name | `upgrade` |
| Retirement depth | code-only, per PR A doc C2, unchanged |
| Rollout | blank-slate reinstall on the test box (ledger lost with the Codespaces workspace) |
| Nextcloud AI config shape | v3.9.x flat app-config keys (`url`/`api_key`/`service_name`) + `models` cache seeding; the v4/v5 services-JSON shape (NC33+) is a follow-on with D2 |
