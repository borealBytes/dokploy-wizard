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
