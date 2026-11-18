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

## Group 5 — Provider + cost visibility for synced models

Operator-driven (showmeseewhy.com): the Hermes Desktop picker lists "Gemma 4 31B
IT:Free" and the default-chat row, but nothing says whether a model is served by
OpenRouter (pay-as-you-go), NVIDIA (free tier), or OpenCode Go (subscription) —
and no surface shows what any of it costs. The gateway already knew the provider:
synced aliases carried it as a path segment (`sync/nvidia/google/gemma...`), but
`/v1/models` exposes only the id and pickers render only the last path segment,
so the provenance half stayed invisible; cost metadata was never written at all,
and LiteLLM reported an implicit 0/0, which reads as "free" even for priced
endpoints.

Fix, one mechanism on every surface (Hermes, the Dokploy AI rows, and the
Nextcloud picker all display the alias id verbatim):

- Aliases flatten the provider into one segment: `sync/<provider>-<model-id>`
  with the id's `/` written as `->` (e.g. `sync/nvidia-google->gemma-4-31b-it:free`).
  Provider leads, so it survives display truncation; the `->` keeps the
  org/model boundary readable; the `:free` tail keeps its meaning (registry-
  verified zero cost).
- Cost threading: each deployment records `input_cost_per_token` /
  `output_cost_per_token` when a catalog can price the model — OpenRouter ships
  `pricing.prompt/completion` (USD/token) on its own catalog rows; NVIDIA and
  OpenCode Go are priced from models.dev (`cost.input/output`, USD/**million**
  tokens, converted down). Unpriced models omit the fields rather than claiming
  zero, so spend reports stay honest and the free-claim logic is unchanged.
- The sources document drops the now-derived `alias_prefix` field and renames
  `free_catalog_url`/`_provider` to `cost_catalog_url`/`_provider`: one registry
  fetch serves both the NVIDIA zero-cost gate and the price metadata.
- `sync/default-chat` keeps its stable name and gains the winner's price; when
  only the price is stale (the deployment predates the metadata — exactly the
  live-box state), the sidecar registers the priced deployment and retires the
  unpriced one without moving the target (`costs_refreshed` in the marker).
- Migration is self-healing: prune/allowlist matching is `sync/`-prefix based,
  so the first cycle creates the flattened set and prunes the nested-shape set
  (provider attribution accepts both shapes, longest provider name first);
  wizard reconcile re-seeds the Dokploy rows on the next pass. One-time cost:
  previously-saved picks in Hermes need re-selecting.

### Group 5 implementation notes (landed 2026-11-18)

- `templates/litellm/model-sync/sync.py`: `alias_slug`, `_catalog_costs`,
  `_models_dev_costs` (replaces `_free_model_ids`; free ⇔ registry-priced zero
  on both sides, unreadable rows fail closed), `source_model_ids` now returns
  `(ids, costs)` from the same fetch, `desired_deployments` builds flat names
  and attaches costs, `create_deployment` emits the cost fields,
  `_alias_source` attributes both alias shapes, `gateway_model_names` records
  existing costs so default-chat can refresh them.
- `src/dokploy_wizard/litellm/model_sync.py`: field renames + `cost_catalog_*`
  on the opencode-go source; README documents the new scheme and the cost fields.

## Review fixes (post-G5, `ocr` pass over the full branch)

An AI code review (`ocr` v1.13.12 over local sglang, `103e397..HEAD`) produced 30 findings;
triage confirmed seven real defects plus a cheap-hardening cluster, and rejected the rest
(style claims that `ruff` disproves, deliberate sync-cost-propagation scope, Access
scaffolding that earns its keep). Landed as one `fix:` batch:

- **Apply-noop upgrade defect** (the review's most serious hit): `run_upgrade_flow` passed
  the apply through modify classification, and the pin file lives outside the env, so a
  re-pin alone classified `noop` and redeployed nothing. The earlier live proof only passed
  because the box already ran the target tag. Fix: `force_phase_into_plan()`
  (`lifecycle/changes.py`) promotes `noop`→`modify` (or folds into an existing plan) for a
  named phase; `run_upgrade_flow` forces `shared_core`. The hash-clear remains
  belt-and-braces.
- **OTP is opt-in** (user call): every shipped surface has its own login, and the admin was
  paying OTP + app-login double-hop. `_resolve_access_otp_emails` no longer infers from
  `DOKPLOY_ADMIN_EMAIL`; `CLOUDFLARE_ACCESS_OTP_EMAILS` alone turns the phase on. The
  machinery stays for future surfaces without their own login. Opting out on a modify run
  takes the Access application and policy down via the API (the OTP provider is Cloudflare's
  undeletable account built-in, so its ledger entry is dropped without an upstream call),
  and the classification guard treats access-disable-only as a disable-only modify.
- **ROOT_DOMAIN rotation + Access**: the phase is scheduled when opted in, so the policy
  re-points at the derived admin hostname on the new domain instead of gating a ghost.
- **Timeouts/`TimeoutExpired`**: `run` helpers around dokploy/remote probing catch it and
  report as failures instead of aborting the flow; the snapshot `pg_dumpall` exec runs under
  its own long timeout rather than the 60 s default.
- **Snapshot hygiene**: `pre-upgrade-*` directories land `0700`, dumps `0600` — the dump
  holds every shared-core secret.
- **`ai.delete` strictness**, 429 body close on the key API, `stable_tag_sort_key` ordering
  fix, generated-key state prunes consumer aliases the build no longer issues, coder
  `list` env passthrough, and `--check` no longer demands a confirm file (read-only).
- **Dead code**: retired `_OPTIONAL_PHASE_PACKS`, nvidia env helpers,
  `_require_optional_positive_int`.
- **Picker costs in templates**: both consolidated coder templates now read
  `model_info.input_cost_per_token`/`output_cost_per_token` echoed by the gateway's
  `/v1/models` (Group 5 attaches them), so flat `sync/<provider>-<model>` aliases price
  without reverse-mapping; the registry fallback map keeps only passthrough prefixes.

## FileX pack + SeaweedFS S3 de-exposure

`filex.showmeseewhy.com` answered 404 because nothing owned the route: the coder
wildcard `*.showmeseewhy.com` swallowed every unrouted subdomain, and FileX was only
ever a wish. This batch ships the pack for real and, in the same pass, pulls the S3
endpoint back off the public internet.

### The pack

- Catalog: `filex` (`ENABLE_FILEX`), `depends_on=("seaweedfs",)` — selecting filex
  auto-selects SeaweedFS; one hostname `filex.<root>` (`FILEX_SUBDOMAIN`);
  `mutable_env_keys=()` because every knob is fixed-by-design.
- Lifecycle: phase `filex` between `docuseal` and `coder` (needs no shared-core
  postgres — FileX carries its own SQLite in `/data`); full ledger/data-volume/
  uninstall/inspect/verify wiring, mirroring DocuSeal.
- Compose renders the upstream image `ghcr.io/brf-tech/filex:latest` with the
  first-boot seed env verified against upstream CONFIGURATION.md/BACKEND.md:
  `FILEX_PUBLIC_URL`, `FILEX_ADMIN_EMAIL`/`FILEX_ADMIN_PASSWORD` (same credentials
  as Dokploy and Coder — the username `admin` is FileX's reserved seed account),
  `FILEX_SECRET_KEY` (derived per stack as sha256 hex, 64 chars ≥ the 32-byte
  requirement, never a literal in the compose file),
  `FILEX_DEFAULT_STORAGE_DRIVER=s3` + bucket/prefix/endpoint/region/keys/path-style,
  `FILEX_TRUSTED_PROXIES=auto`, `FILEX_VERSIONS_ON_OVERWRITE=1`,
  `FILEX_TRASH_RETENTION_DAYS=30`. All of these are first-boot seeds only: operator
  edits inside FileX win afterwards (`filex settings reseed` is the escape hatch).
- Bucket contract (constants, no env override): bucket `filex`, prefix `files`,
  region `us-east-1` — FileX refuses an empty prefix and needs the region explicit.

### Bucket + versioning provisioning

`DokployFilexBackend` talks S3 with a minimal SigV4 client (the flow proven live on
the box by `s3_ver_probe.js`): path-style requests always to
`https://127.0.0.1` with an explicit `Host:` of the internal s3 route — UFW blocks
host→overlay TCP, so the loopback edge is the only host-side door, and traefik
answers unknown SNI with its default cert (verification off). Provisioning is a
retry loop (12 × 5 s) — head the bucket, create on 404, read versioning, PUT
`<Status>Enabled</Status>` — so it converges even while the route is still
propagating, then fails loudly rather than half-provisioning. Once per reconcile
it probes `POST /api/auth/login` through the public route: 200 proves the seeded
admin; 401/403/429 are recorded as notes, not failures, because FileX locks an
account after 5 wrong attempts and stored DB values outrank the env anyway.

### The de-expose

`seaweedfs` now models **no public hostname**: the S3 gateway on this deployment
is anonymous-by-default (no `s3.json`), and an open bucket on the internet is an
abuse invitation. The gateway serves on `<stack>-s3.internal` — a traefik Host
rule on the same tunnel edge, resolvable only from cluster containers and from
the host through the loopback trick.

Old boxes carried an owned `s3.<root>` CNAME. The networking phase now ends with
a generic DNS prune: owned records whose hostname left the desired state are
deleted **only if the zone still shows them pointing at this tunnel's target**
(an operator who repointed the name keeps their record; a vanished record just
drops its ledger entry; upstream 404s are tolerated so the prune converges). The
`"s3"` entry in `_HOSTNAME_PHASES` keeps legacy desired states scheduling
networking + seaweedfs when the hostname disappears.

**Future re-exposure** stays an open item deliberately: before `s3.<root>` comes
back it needs an identity story — per-user SeaweedFS credentials (`s3.json` +
identity mapping) and ideally a Cloudflare Access service-token policy in front —
otherwise the next wizard pass will happily keep publishing an unauthenticated
bucket again.

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
- Attempt 3: hung 85+ minutes at `mutate-install` with heartbeats only. Cause
  was the reset itself: removing the bootstrap **services** but not the
  installer-created `dokploy` / `dokploy-postgres` **volumes** means the
  reinstalled bootstrap generates a new Postgres password while the surviving
  data volume keeps the old one — Dokploy crash-looped on
  `password authentication failed for user "dokploy"` and the wizard waited on
  its bootstrap health check. Full teardown (services + task containers + both
  volumes) before attempt 4; AGENTS.md recovery note now spells this out.
  Operator lesson: never pipe the remote helper through `tail` for live runs —
  the pipe buffers everything and the run looks silent even while streaming.
- Attempt 4: install passed bootstrap + networking, died in `shared_core` at
  provider seeding with `Dokploy ai.create response must be an object.` Live
  probe against Dokploy v0.31.4 established the real API-key surface shapes:
  `/api/ai.create` and `/api/ai.update` answer **`[]`** (empty array) after
  writing the row, `/api/ai.getAll` omits `apiKey` entirely, and
  `/api/ai.delete` answers bare `true`. The API-key client's dict assertion
  was wrong for upstream. Fix (commit `1f07417`): accept non-object mutation
  responses and recover the authoritative record from `ai.getAll`, matching on
  name/apiUrl/model/isEnabled (plus aiId for updates), ignoring the never
  echoed apiKey. Attempt 5 relaunched `proof` on the same box but the wizard
  correctly refused: `Existing install state does not match this install
  request. Use 'modify'` — attempt 4 had persisted state before crashing.
  Pivoted to `dokploy-wizard-remote modify` to converge over the healthy
  partial install (this also exercises the modify path twice: first pass
  completes the stack, second pass must be a noop).
- Attempt 6 (modify #1): drift gate blocked on the `litellm-sync` container
  deployed by attempt 4 but never ledger-recorded (crash mid-phase left the
  ledger pre-shared-core; auxiliary tolerance requires the parent litellm
  scope to be managed). Removed the container; the compose definition stayed.
- Attempt 7 (modify #2): full converge in 222s. All phases green; single
  workspace `showmeseewhycom-workspace` (Group 4 stable name live). Live
  probes confirmed: Dokploy `ai.getAll` = exactly the two D1 rows
  (`Dokploy Wizard LiteLLM` default + `· sync/default-chat`), and Nextcloud
  `integration_openai` points at the internal gateway with its own virtual
  key, service name "LiteLLM (Dokploy Wizard)", models cache seeded with the
  default alias, `sync/default-chat`, and the `sync/*` wildcard.
- Attempt 8: the removed sync service did not come back because modify skips
  compose deploys when the artifact hash matches, and the model-sync wait is
  soft-fail. Cleared the hash via `clear_compose_artifact_hash` and reran
  (modify #3): sidecar healthy, cycling (desired=73, liveness probes, key
  reconcile=4), and the wizard's consumer-key push triggered its early
  cycle. Modify #4: true noop — zero create actions with the sidecar
  running, proving drift auxiliary tolerance.
- Attempt 9 (`upgrade --check`): first run crashed following the registry
  Link rel (`unknown url type: '/v2/...?last=...&n=0'`) — ghcr.io paginates
  with path-relative Links. Fixed by joining against the current page
  (commit `5720388`). Second run exposed two more truths: the probe only
  inspected swarm services (everything reported missing — this stack is
  compose), and the echoed n=0 truncated the newest-stable scan to
  v1.83.7-stable. Fixed with a compose container fallback (Config.Image +
  RepoDigests) and forced n=1000 page size (commit `a75f855`). Third run:
  report available, all 11 components ok (running digest == registry
  digest, incl. model-sync), litellm newest-stable correctly v1.83.14-stable
  (no bump pending), workspaces section report-only as designed (coder CLI
  wants a session token). `inspect-state`: status clean, all healthy.
- Upgrade APPLY (same tag, exercising the gate): first apply completed
  (pin written, hash cleared, modify redeploy, rollback hint emitted) but
  the snapshot's `pg_dumpall` leg failed — `DockerCliProbe.exec` forced
  `--workdir /app`, which the postgres image lacks (OCI exit 127). Fixed
  (commit `b096ead`); second apply captured
  `shared-core-postgres.sql.gz` + the state copy, and its plan showed
  `previous_pin_source: state-pin`, proving the written pin outranks the
  env default on the next pass. `fixtures/upgrade.confirm` now ships as the
  typed confirmation file the docs reference.
- Group 5 ALIAS MIGRATION (remote modify, 2026-11-18): sources-doc change
  forced the sidecar redeploy; its boot cycle migrated the live gateway in one
  pass — `desired=73 created=73 pruned=73 create_errors=0 prune_errors=0
  key_reconciled=4` (all four consumer keys), and the immediately following
  early cycle was a true noop (`created=0 pruned=0`). Post-state verified:
  `/v1/models` (hermes key) shows 75 sync aliases, ALL flat
  (`sync/nvidia-google->gemma-4-31b-it:free`), zero nested-shape leftovers;
  49 rows carry nonzero cost — OpenRouter aliases priced from inline catalog
  rows (e.g. `deepseek/deepseek-v4.1-flash` at $0.051/M in, $0.567/M out),
  NVIDIA free endpoints record explicit zeros; `sync/default-chat` refreshed
  in place to OpenCode Go list price ($0.15/M in, $0.60/M out — models.dev)
  without moving its target (`cost metadata refreshed` log line, deployments
  replaced promote-before-retire). Dokploy `ai.getAll` after the pass: exactly
  2 wizard rows, both flat-shaped, enabled; no nested-shape stragglers.
  Bonus field evidence while at it: the liveness probe correctly dropped one
  delisted OpenRouter model and four dead NVIDIA endpoints during the same
  cycle. The `g5-proof-probe` API key used for this read could not be revoked
  (`user.listApiKeys` 404s on this Dokploy build) and joins the hygiene list.
- Known hygiene gaps observed during proof (not blockers, candidate
  follow-ups): every mutating pass creates a fresh run-scoped Dokploy API key
  (`dokploy-wizard-<uuid>`) that is never revoked, and each bootstrap
  qualification leaves an `auth-probe` compose project behind. Both are
  invisible to functionality but accumulate on long-lived boxes.

## Verification matrix

| Scenario | Command | Proves |
|---|---|---|
| Local gates, every commit | `uv run --all-extras pytest -q && ruff check . && mypy .` | AGENTS.md gate |
| Fresh install (after box wipe) | `./bin/dokploy-wizard-remote proof --host 157.173.124.231 --password … --env-file .install-min-testing.env` | slim tree installs; Nextcloud AI picker lists the full synced set; Dokploy Ask-AI lists every alias; Hermes Desktop tile regression (browser/CDP check, not curl — PR A doc A3b CSP lesson) |
| Modify ×2 | `…-remote modify …` twice | idempotent, one workspace (proves Group 4) |
| Drift report | `…-remote upgrade --check …` | digest-resolved table; closes the "newest `litellm:*-stable` + CVE list" backlog item |
| Upgrade apply | `…-remote upgrade … --litellm <approved tag>` + one forced failure | real redeploy on a pinned component; ledger rollback restores previous tag |
| Survivor regression | browser + CLI pass | coder/Hermes tile, community WebUI, nextcloud, moodle, docuseal, seaweedfs, Access in front of litellm |
| FileX first serve + de-expose | `…-remote modify …` with `PACKS=…,filex` | `https://filex.<root>/healthz` 200, login with Dokploy creds, `filex` bucket default storage with versioning Enabled, `s3.<root>` tunnel route dead (terminal 404) + owned CNAME pruned, second pass noop |

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
| Provider/cost visibility | Group 5 (user-requested): provenance goes INTO the alias (flat `sync/<provider>-<id>`, `/`→`->`) because clients truncate paths; costs ride LiteLLM's standard `model_info` fields from the catalogs the sync already consumes — no dependence on any picker's UI |


## Phase 6b (2026-11): FileX pack, internal-only SeaweedFS S3, per-workspace Coder keys, HTTPS-only edge

Four additions landed after the initial live-run checklist, all proven or gated by
tests + a fresh remote proof:

### FileX pack (`filex.<root-domain>`)
- New pack: `brf-tech/filex` image, single replica, bind mount at
  `<stack data dir>/filex`, shared `seaweedfs-data` volume for the SeaweedFS client mount,
  `DockerProxyNetwork`, and a wizard-owned `filex` tunnel route.
- SeaweedFS backend: bucket `filex`, prefix `files`, region `us-east-1`, credentials are the
  pack's generated secrets persisted under `filex.generated-secrets.json`.
- Bucket bootstrap (`filex_bucket_bootstrap.py`): SigV4 PUT bucket + versioning, idempotent,
  runs after the service is healthy. Versioning is enabled from the first boot so FileX's
  native version history works without user setup.
- Health probe: `/healthz`.
- First-login seeding: admin account matches the shared Dokploy/Coder admin credentials
  (email + password), FileX-native versioning on, default storage pointed at the mounted
  SeaweedFS bucket/prefix — the workspace is usable without a setup wizard.
- Live proof on `showmeseewhy.com`: `https://filex.showmeseewhy.com/healthz` 200
  (`{"status":"ok"}`) through the tunnel and via loopback + Host header; login POST with the
  Dokploy admin credentials returns 200 (user `admin`, id 1); bucket HEAD + versioning GET
  return 200/`Enabled` over the loopback SigV4 probe.

### SeaweedFS S3 de-exposed
- The `s3.<root-domain>` public CNAME is retired: Nextcloud (embedded client), Moodle
  (external), and Coder (external) all use the internal container hostname. A new
  `internal_hostname` field on `ManagedSeaweedFS` records `http://<stack>-seaweedfs.internal:8333`
  and the state digest now covers it. The reconcile pass deletes an owned-and-stale
  public S3 route (404 on public `s3.` after the next apply, proven live); the internal
  hostname keeps serving (200 on the loopback probe).
- If public S3 is ever re-enabled (identity config), that is a deliberate later task.

### Per-workspace Coder LiteLLM keys (scoped issuer + template mint)
- New generated consumer `coder-workspace-issuer` (auto-backfilled into existing
  generated-keys state). It is not a chat consumer: it is what each workspace uses to mint
  its own virtual key. Validation is subset-based, so the issuer's team allowlist is
  deliberately `("*",)` (can mint any gateway model); its key is pushed with
  `allowed_routes = /key/generate,/key/info,/key/delete` so it can mint but never chat.
- The issuer key is excluded from the sync sidecar's consumer-keys push (which merges
  explicit model lists and would replace the wildcard).
- The five gateway-key Coder templates (all except kdense-byok, which brings its own key)
  gained a startup mint: a deterministic child token
  `sha256(issuer + ":" + workspace_name)`, `key_alias = workspace name`, models
  `sync/*, sync/default-chat`. Info-then-generate-then-re-info: adoption after restarts,
  issuer rotation re-mints for free, and any failure (403, alias collision, gateway
  unreachable) leaves the baked shared template key in place, so AI still works.
  A workspace-level `AI_DEFAULT_API_KEY` (hermes-family: `HERMES_TEMPLATE_API_KEY`)
  env override still wins over the minted key.
- LiteLLM spend now attributes per workspace via the key alias; rotating one workspace's
  key does not touch the others.

### HTTPS-only edge (box-local Traefik redirect, two halves)
- The zone setting is deliberately NOT touched: `Always Use HTTPS` is zone-wide state, and it
  would also silence plain HTTP for Coder workspace wildcard routes, where serving HTTP can be
  a legitimate choice. The tunnel re-encrypts every visitor request to origin
  `https://localhost:443` (`noTLSVerify`), so the only visitor-scheme signal at the box is
  cloudflared's `X-Forwarded-Proto` header. Enforcement lives on this box, in `edge_https.py`:
  1. **Static trust patch** — Dokploy's `/etc/dokploy/traefik/traefik.yml` is patched
     idempotently with `entryPoints.websecure.forwardedHeaders.trustedIPs` (loopback +
     RFC1918, because swarm's userland proxy makes :443 peers gateway addresses; verified
     live: without trust, Traefik overwrites client `X-Forwarded-Proto` before rule matching,
     and with it the header rule fires and the request scheme becomes `http`). Traefik is a
     service, so a static change rides a `docker service update --force dokploy-traefik`
     (fallback `docker restart`); a failed restart warns, never fails the phase.
  2. **File-provider drop-in** — `dokploy-wizard-edge-https.yml` rendered into the watched
     dynamic dir: per wizard hostname a priority-1000 router
     `Host(...) && Header(\`X-Forwarded-Proto\`, \`http\`)` (Traefik **v3** rule name — v2's
     `HeadersRegexp` was removed and such rules fail open, silently never matching) on web +
     websecure, attached to a permanent `redirectScheme` middleware (trust makes the scheme
     `http`, so it fires and Traefik builds the Location from the real host/path — a manual
     `redirectRegex` earlier produced mangled Locations) and a discard service.
- Plain HTTP 301s to the HTTPS URL before any app is dialed; `https://` requests fall through
  to the normal routers; wildcard routes (Coder workspaces) are absent from the file and keep
  plain HTTP. Writes are content-stable; dry runs note intent only; a box without a Traefik
  install warns once and never fails the phase. Uninstall removes the drop-in. This also fixes
  the Dokploy NextAuth "Invalid origin" failure mode: the http:// login surface no longer
  exists.

### Gates
- `pytest` (1079 passed), `ruff check`, `mypy` green. Template changes live entirely
  inside `<<-EOT` heredoc bodies, so Terraform formatting is untouched.


## Phase 6c (2026-11): template collapse, kdense retire, honest mint guard

### One template to rule them all
- The wizard now seeds exactly one template, `ubuntu-vscode-hermes-desktop`. The other five
  (`ubuntu-vscode`, `ubuntu-vscode-opencode-web`, `ubuntu-vscode-kdense-byok`,
  `ubuntu-vscode-hermes`, `ubuntu-vscode-opencode-hermes-pi`) moved to the retire list; their
  directories are deleted.
- Retirement migrates first: `_migrate_retired_template_workspaces` re-bases every workspace
  riding a retired template onto the survivor via `coder update --template --yes` BEFORE
  `coder templates delete`. The home volume is `coder-<workspace-id>-home` with
  `ignore_changes = all`, so an in-place template switch preserves `/home` while a
  delete-and-recreate would silently hand the user an empty home.
- The `coder-kdense` virtual-key consumer retired with its template. `reconcile_virtual_keys`
  gained a prune pass: wizard-managed key/team records outside the desired consumer set are
  deleted (`delete_team` added; `POST /team/delete`); records without the wizard metadata
  marker are the operator's and stay untouched. State repair prunes the retired consumer from
  the generated-keys file like any ghost name.

### Mint guard fix (live proof caught it)
- The mint guard compared the issuer assignment against a second copy of the
  `__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY__` marker. Template substitution replaces every
  occurrence, so post-substitution the guard compared the real key to itself and minting was
  skipped on every boot. The guard now compares against
  `__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY_SENTINEL__`, which is deliberately absent from the
  replacement map (the Hermes wiring already used that trick). A regression test substitutes
  a fake issuer into the template text and asserts no guard line ends up comparing the
  variable to its own substituted value.

### Gates
- `pytest` (1055 passed), `ruff check`, `my` green.
- Live: modify on the box re-seeds one template, migrates both existing workspaces (already
  on the survivor from the 6b proof), retires the five ghosts, prunes the kdense key/team in
  LiteLLM, and the rebuilt workspace mints its child key (verified via `/key/info`).
- The zone `Always Use HTTPS` blocker is obsolete: the redirect moved to a box-local Traefik
  drop-in (see "HTTPS-only edge" above), so the API token needs no Zone Settings grant and
  the zone itself is never modified — Coder workspaces keep the option of plain HTTP.

### Follow-ons (next slices, approved in planning)
- ~~FileX image pin bump~~ resolved: the pack shipped modern (`brf-tech/filex:latest`, and
  the box resolved it to the v0.61.2 digest — audit-log S3-key attribution is available).
  The tag is now an immutable digest pin (`v0.61.2@sha256:e8756486…`) so reruns land on
  the same bytes; live compose convergence re-proves it on the next modify.
- `~/shared` rclone-FUSE mount of the FileX bucket in the surviving template: per-(user,
  workspace) FileX S3 key, `--vfs-cache-mode full` hot cache (defaults 10 G / 48 h via
  `SHARED_CACHE_SIZE`/`SHARED_CACHE_AGE`), `shared-versions` + `shared-cache` helpers.
- ~~Gitea pack~~ implemented as Phase 7 below: rootless image, repos on the data volume,
  `gitea` bucket for LFS/attachments, internal SSH :2222, git-over-HTTPS on the public route.
- Identity automation: Coder as OIDC IdP for Gitea/FileX (accounts spring up at first
  login), workspace-boot provisioner for owner account + per-(user,workspace) FileX key +
  deterministic account-level Gitea SSH key, deleted-user disable, LiteLLM mint re-scope to
  `<login>-<workspace>`.


## Phase 6d (2026-11): ~/shared — the FileX bucket as a live workspace folder

### The door
- Spike-proven on the box: rclone FUSE mount over FileX's own S3 door
  (`http://{stack}-filex:5212/s3`, path-style, bucket = the storage name
  `Files`) beats native `filex mount` — rclone carries the disk-backed VFS hot
  cache the requirement asks for (names visible, content on first open, idle
  eviction; `--vfs-cache-mode full`, defaults 10 G / 48 h via
  `SHARED_CACHE_SIZE`/`SHARED_CACHE_AGE` in `~/.config/dokploy/shared-mount.env`).
  `--vfs-cache-poll-interval` is unsupported on S3 remotes — dropped.
  The remote must be `provider = Other`: with `provider = AWS`, rclone v1.76's
  AWS-SDKv2 flexible-checksum path dies on "failed to seek body to start"
  before the PUT is sent (the `--s3-disable-checksum` flag does not bypass
  it); `Other` signs plain PUTs the door accepts. Confirmed live end-to-end:
  write through `~/shared` -> door -> FileX index row owned by the person ->
  overwrite snapshot -> `shared-versions --restore` -> content back.
- The FileX compose now also attaches to the shared overlay (`{stack}-shared`,
  external): the service was unreachable by DNS from workspaces before this
  (spikes could only reach it by container IP).

### FUSE inside the workspace container
- `docker_container.workspace` gains `privileged = true` and the `/dev/fuse`
  device. The provider Coder ships (kreuzwerker/docker v4.7.0) has no
  `cap_add`/`capability` support at all, so privileges cannot be narrowed; the
  docker-default AppArmor profile otherwise denies `mount()` to fusermount3
  ("mount failed: Permission denied", measured live). Single-tenant box: the
  workspace container is already the user's own machine.
- A `flock`-guarded supervisor loop remounts when the mount dies and re-reads
  the env file each cycle, so `shared-cache <size>` (rewrites the cap and
  unmounts) takes effect within ~20 s without restarting anything.
- The whole bootstrap runs backgrounded inside the agent startup script: no
  identity, download, or FUSE failure can take a workspace boot down.

### Identity and attribution
- The pack provisions `wizard-provisioner` (admin role) + one admin-scoped API
  token per pass (`read,write,delete,admin`). Measured: FileX refuses
  `/api/admin` for plain tokens ("delegated administration is available to a
  signed-in session only"), admin-scope tokens can create users and mint child
  tokens, and `list` is **not** an issuable scope (valid: read, write, delete,
  mcp, admin, root:<storage>://<folder>).
- Workspace boot (template `FILEXPROV` block): ensure the owner's user (role
  `user`, password `sha256(issuer + ":" + login)`, email `<login>@coder.wizard.local`),
  sign in as the owner, then label-match/delete/re-mint the per-(user,
  workspace) S3 key `<login>-<workspace>` and a `read,write` helper token
  `coder-<login>` (`/api/ai/versions/restore` is the versions API; restore is a
  write, hence no narrower scope exists). The provisioner retires from the
  path immediately — every write carries the person's own credential. A manual
  account with the same name is never reset: the mount skips and logs.
- Protocol-door (S3) writes feed the FileX catalogue live (measured): the file
  row appears in `/api/files/manager?action=index` immediately with owner =
  the writing S3 key's owner, and an overwrite snapshots the previous body
  into the `.versions` catalogue (`storage_key .versions/<node>/<n>`) exactly
  like REST-door writes. They emit no `audit_log` rows (v0.61.2 middleware
  covers REST doors only), so attribution for `~/shared` writes is the file
  row's owner, the key label (the workspace), and the version-catalogue
  snapshots. Audit webhooks remain the upgrade path if row-level audit for
  protocol doors ever matters. The S3 `?versions` sub-resource is answered
  `501` on the door — history rides `/api/ai/versions`.
- `FILEXPROV` writes `shared-mount.env` (0600, access key + secret + helper
  token) and `~/.config/rclone/rclone.conf` (0600); both live on the home
  volume, and the secret adoption path (reuse while the stored key
  authenticates) keeps restarts from churning credentials.

### Convergence
- A template push alone leaves the running workspace on its old build; the
  proof needed a manual `coder update` before the mount appeared. So
  `_ensure_default_workspace` now ends with `_update_workspace_if_outdated`:
  `coder list -o json`, and when the default workspace on the default template
  reports `outdated`, run `coder update <owner>/<ws> --yes`. Only the wizard's
  own default workspace is touched — user-created workspaces keep their own
  rebuild timing. The home volume (`ignore_changes = all`) survives the
  in-place update, and the VFS cache on it resumes pending uploads.

### Guards
- Same sentinel discipline as the issuer fix: the mount activates only when
  `__DOKPLOY_WIZARD_FILEX_INTERNAL_URL__` and
  `__DOKPLOY_WIZARD_FILEX_PROVISIONER_TOKEN__` were substituted (the
  `*_SENTINEL__` markers are deliberately absent from the replacement map), so
  a FileX-less stack or a raw pushed template renders a plain `~/shared`.

### Gates
- `pytest` (1058 passed), `ruff check`, `mypy` green; provisioner lifecycle,
  compose network, replacement-map, and sentinel-regression tests added.


## Phase 7 (2026-11): Gitea pack

### Shape
- New pack `gitea` in the catalog (`ENABLE_GITEA`, default off, subdomain `gitea`,
  `depends_on=("seaweedfs",)`, `shared_core_requirements=("postgres",)`). Full lifecycle
  wiring: phase after filex, drift handler mirroring filex, uninstall rules (service 34 /
  data 35, phase ordered after filex and before seaweedfs so the bucket outlives the app),
  service-verification candidate (`_verify_current_application`), inspect-state candidate.
- Official **rootless** image, digest-pinned: `gitea/gitea:28.4.0-rootless
  @sha256:15ab7fadb4…073a` (same digest serves `28.4-rootless`/`latest-rootless`; v28.4.0
  is the current stable). Run as `1000:1000`; the rootless image ships no OpenSSH, so the
  **built-in SSH server** listens on `2222` inside the overlay only (`START_SSH_SERVER`,
  `SSH_LISTEN_PORT`/`SSH_PORT=2222`) — no publish, no tunnel route; git-over-HTTPS rides
  the public hostname. Publishing 2222 is an operator choice, not a wizard default.
- One named volume carries both roots (`/var/lib/gitea` and `/etc/gitea` on the same
  volume — harmless coexistence, keeps a single ledger data resource). Repos live there;
  **LFS objects and attachments are redirected to a wizard-owned `gitea` bucket** on the
  stack's SeaweedFS gateway. Schema is the modern (1.20+) one, verified live:
  consumer sections carry `STORAGE_TYPE = minio` + `MINIO_BUCKET`
  (`GITEA__attachment__*` / `GITEA__lfs__*`) and the shared
  `[storage.minio]` section carries the `MINIO_`-prefixed connection
  (`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY_ID`, `MINIO_SECRET_ACCESS_KEY`,
  `MINIO_USE_SSL`, `MINIO_PATH_STYLE`, `MINIO_LOCATION`). The pre-1.20
  spellings are silently ignored — with them in place the box logged
  `storage with type: local` and no warning. So the repo volume stays small
  and backup-friendly. Bucket create rides the same
  `LoopbackSigV4Client` lane FileX uses (UFW blocks host→overlay). No versioning — Gitea
  tracks object identity itself; unlike FileX the bucket is create-only.
- Config fully via `GITEA__section__KEY` env with `INSTALL_LOCK=true` and
  `DISABLE_REGISTRATION=true`: no web installer, no signup. `ROOT_URL` is the https
  hostname; the `forwarded-https` middleware stamps `X-Forwarded-Proto/Host/Port` so
  clone URLs match `ROOT_URL`. Mailer wires to the shared postfix relay when present
  (`ENABLED=true`, `SMTP_ADDR={stack}-shared-postfix` + `SMTP_PORT=587` —
  the address is looked up as a bare hostname, so a host:port value fails
  resolution with `no such host`), else disabled. Health gate is
  the native `/api/healthz` (`ENABLE_HEALTHCHECK=true`), probed over the loopback edge.
- `GITEA__security__SECRET_KEY` is derived per stack+ref (`sha256("dokploy-wizard:gitea:
  <stack>:<ref>")`) and kept out of the compose file behind the `${VAR:?…}` placeholder,
  exactly like the FileX secret-key base. DB password is the shared-core literal
  `change-me` (the DocuSeal convention: every pack role shares it; overlay-only network).

### Provisioner account
- An install-locked Gitea has **no signup path**, so the wizard's `dokploy-wizard` admin
  is created through the in-container CLI: `docker exec --user 1000 <container> gitea -c
  /etc/gitea/app.ini admin user create … --admin --must-change-password=false`, with a
  deterministic password (`sha256` of stack+secret-ref material). The CLI create tolerating
  "already (been) taken" is what lets a lost state document converge on the next pass.
- One API token (`dokploy-wizard-provisioner`, scope `all`) is minted once
  via basic-auth `POST /api/v1/users/<u>/tokens` over the loopback edge and persisted to
  `gitea-provisioner.json` (0600) in the state dir. Every later pass verifies the token
  against `/api/v1/user`; only when it stops authenticating does the pass delete the stale
  token by name and re-mint (409 → delete → POST). A failed CLI create degrades to a
  `WARNING:` note + `provisioner_ready=false`, letting the health gate own the verdict.
- This token is the credential the next identity slice (Coder-as-OIDC-IdP account
  springing, deleted-user disable) drives the API with.

### Wiring
- Service attaches to the shared overlay (`{stack}-shared`, external) so Coder workspaces
  clone over `http://<stack>-gitea:3000` without leaving the host.
- Apply path reuses the filex/docuseal pattern: `apply_compose_noop_guard` + artifact-hash
  persistence, so reruns on unchanged renders deploy nothing.

### Gates
- `pytest` (1099 passed, 1 skipped), `ruff check`, `mypy` (179 files) green.
- `tests/unit/test_gitea_pack.py`: 19 tests — reconciler skip/requires/allocation/dry-run
  bindings/applied-ledger/owned-reuse/health-fail; render assertions (pin, rootless user,
  dual-mount, install lock, ssh, bucket endpoint w/ scheme stripped, mailer on/off, shared
  network, env-spec values + hash anchor); `ScriptedS3` bucket create/idempotent/retry/
  give-up paths; provisioner bootstrap/alive-token short-circuit/rotation with
  already-taken CLI/degrade-to-warning via `ScriptedCli` + `_ScriptedGiteaHttp`.
- Two pre-existing expectations updated for the new pack: catalog name list, and the
  `LifecycleBackends` construction in `test_cli.py` now carries `gitea=`.
- Live proof history on the box (log `/tmp/opencode/modify19..21.log` + probes):
  - modify19: classifier rejected `ENABLE_GITEA` — `_SUPPORTED_ENABLEMENT_KEYS`/
    `_SUPPORTED_HOSTNAME_KEYS` were hand-maintained lists that had drifted (missing
    gitea AND seaweedfs); both now derive from `iter_pack_catalog()`.
  - modify20: phases ran `docuseal,filex,gitea` without `shared_core` — `_new_pack_phases`
    was a hardcoded map. Gitea crash-looped `pq: password authentication failed (28P01)`
    because the per-pack role is live-provisioned by the shared_core phase (init scripts
    only answer a first boot). The function is now catalog-derived: a new pack schedules
    its own phase plus `shared_core` when it declares requirements. Regression test added.
  - modify21: gitea `outcome: applied`, health passed, bucket ready, ledger written —
    only `provisioner_ready: false` (see scope grammar). Container exec lookup also had
    to move to the compose-service-label pattern (Dokploy names containers
    `{project}-{service}-{replica}`, not swarm `<service>.<slot>.<task>`).
  - Box-measured API truths: token `scopes` accept ONLY the bare `"all"` — `all:all`,
    `all:write`, `write:all`, `admin:write` → 400 (earlier 422s were JSON-string forms);
    `SMTP_ADDR` must be host-only + `SMTP_PORT` (the whole `host:port` string is DNS
    looked up as one name → warning; postfix's alias on the shared network is
    `{stack}-shared-postfix:587`); and the pre-1.20 storage keys are silently ignored —
    the modern consumer/`MINIO_`-prefixed set above was proven live by patching app.ini
    in place: `Initialising Attachment/LFS storage with type: minio`,
    `Creating minio storage at <gw>:8333:gitea`, healthz 200.
  - Convergence on the box went through the modify resume lane (proof's
    mutate-install lane refuses a post-modify box by design: the install-time
    raw predates `ENABLE_GITEA`, so it raises "Use 'modify'"). Truncating
    `completed_steps` to the `filex` prefix — the exact shape a failed phase
    leaves behind — made the next modify genuinely rerun `gitea`: compose
    redeployed with the corrected env, provisioner account seeded, token
    minted with the bare `all` scope, `gitea-provisioner.json` written 0600,
    `provisioner_ready: true` with no WARNING note. Box-verified after the
    rerun: token authenticates at `/api/v1/user`, deployed container env
    carries exactly the measured key set, boot logs show Attachment and LFS
    storage initializing as `minio` against the live bucket with the mailer
    warning gone, and the public `https://gitea.<root>/api/healthz` serves
    200 over the HTTPS-only edge.
  - The follow-up noop check then exposed a pre-existing convergence bug
    outside the gitea lane: the preserved-phase drift probe compared the live
    Cloudflare tunnel ingress against a route set built WITHOUT the on-demand
    LiteLLM admin hostname, while the planner pushes it WITH that route —
    so every noop-classified modify flagged drift at `networking` and
    reran the whole chain forever. The probe now builds its expectation with
    `resolve_litellm_admin_hostname`, parity-pinned by a new drift test
    (accept-ingress-including-admin-route) alongside the existing
    stale-ingress rejection.
  - With `networking` fixed, the noop attempt surfaced the next masked
    drift, at `shared_core`, and it was the same class of bug: the
    virtual-key validator compared the wizard's raw key from
    `litellm-generated-keys.json` against the live `/key/list` record key —
    but LiteLLM's list payload carries no raw token at all. Box-measured:
    every wizard-managed alias returns `token == sha256(raw)` (a 64-hex
    digest), `api_key: None`, and a masked `key_name` (`sk-...epbb`);
    `_parse_key` falls back to the hash, so raw-vs-hash never matches and
    every noop-classified modify would rerun `shared_core` forever. The
    reconciler had the same comparison, which additionally churned
    delete+recreate on all four consumer keys on every real pass. Both sides
    now prove the match with `_matches_generated_key` (raw equality OR
    `sha256(expected) == record.token`), pinned by tests feeding
    hash-only records to the validator and the reconciler.
