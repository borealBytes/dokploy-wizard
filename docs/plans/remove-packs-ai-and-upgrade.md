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
- New pack `gitea` in the catalog (`PACKS` name `gitea`, default off, subdomain `gitea`,
  `depends_on=("seaweedfs",)`, `shared_core_requirements=("postgres",)`; the launch-era
  `ENABLE_GITEA` flag lives on one release longer as a deprecated alias — the resolver
  folds it into the same enabled set and stamps a stderr deprecation note). Full lifecycle
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
  - With `shared_core` also passing, the noop attempt surfaced a third
    masked probe bug, at `docuseal`: the preserved-phase check compared
    `bootstrap_state.initialized` truthily, but the dry-run lane of
    `reconcile_docuseal` reports `initialized=None` (unknown without a live
    check — only the applied lane verifies it via
    `ensure_application_ready`), so `not None` read as drift and every
    noop-classified modify resumed from `docuseal` forever. The probe now
    drifts only on an explicit `False`, pinned by a pair of drift tests
    (accept unknown / reject False). The box-side full-set probe then
    reported every preserved phase ok, and the following plain modify
    finally measured the true noop: `"mode": "noop"`, `"phases_to_run": []`
    (all ten phases preserved, health outcomes `already_present`),
    completed in 22s with no phase reruns — the full
    install→modify→noop lifecycle holds with the Gitea pack enabled.

## Phase 8 (2026-11): identity automation — Gitea accounts spring with workspaces

### The OIDC verdict (measured, blocks the literal lane)
- The original slice-5 sketch was Coder-as-OIDC-IdP → Gitea `source_id`
  accounts. Box-measured at the pinned Coder **v2.39.4**: `CODER_OAUTH2_PROVIDER_ENABLE=true`
  flips `buildinfo.oauth2_provider`, but the provider is RFC 8414 **OAuth-only**:
  discovery lives at `/.well-known/oauth-authorization-server` (authorize/tokens/
  revoke, PKCE S256, opaque tokens, **no id_token**), and
  `/.well-known/openid-configuration` serves the dashboard SPA HTML (the dashboard
  fallback router owns that path). DCR is off by default. Gitea's only generic
  auth-source type is OIDC-with-discovery → **Coder cannot be Gitea's OIDC
  provider at this pin**; the lane reopens when Coder ships OIDC discovery.
- So identity automation rides the provisioner token the Gitea pack already
  seeded: boot lanes spring accounts, the coder phase disables them, and the
  per-workspace LiteLLM identity is fixed for cross-owner collisions.

### Boot lane: GITEAPROV (template)
- New background subshell alongside the ~/shared lane, guarded by the two new
  replacements `__DOKPLOY_WIZARD_GITEA_INTERNAL_URL__` /
  `__DOKPLOY_WIZARD_GITEA_PROVISIONER_TOKEN__` (sentinels unmapped, same trick;
  empty when the pack is off → lane skipped silently, no boot noise). Door is
  the overlay service DNS `http://{stack}-gitea:3000` — compose exposes 3000
  on the external shared network, workspaces never leave the host.
- Account spring: username = coder login, email = `{login}@coder.wizard.local`
  (the FileX lane's convention), password derived `sha256(seed + ":" + login)`
  with the same seed rule (issuer key, else provisioner token). Created through
  `POST /api/v1/admin/users` with `must_change_password:false` — box-measured:
  a pending must-change makes every basic-auth API call a 403, so a boot lane
  could not manage its own key. A manual account that owns the name is left
  alone (email mismatch → skip, never reset).
- One SSH key per (user, workspace): `ssh-keygen`ed on first boot into the home
  volume (`~/.ssh/id_ed25519_gitea`), registered by title `{login}-{workspace}`
  via `POST /api/v1/admin/users/{u}/keys` (measured: the admin route is
  POST-only, GET → 405; `GET /api/v1/users/{u}/keys` answers for the token).
  Adoption is by key material, replacement retitles through the owner's own
  basic auth (a human-set password then harmlessly leaves the stale key).
- Convenience: ssh_config Host alias `gitea` → overlay name port 2222 (the
  built-in SSH server, overlay-only) with a keyscanned known_hosts pin, and
  `git config --global user.name/email` defaults set only when unset.
- Re-enable lane closes the loop: if the account exists but is inactive (the
  coder phase disabled it when the Coder user was deleted), the next boot
  PATCHes it active again.

### Disable lane: coder-phase identity sync
- `ensure_application_ready` runs `_sync_gitea_identities` after template
  seeding: Coder users via `GET /api/v2/users` (session token) ∩ Gitea via
  `GET /api/v1/admin/users` (provisioner token, loopback edge). Scope is the
  email convention alone — manual accounts and the provisioner are invisible
  to it. Present → active, absent-or-deleted → inactive, applied with
  `PATCH /api/v1/admin/users/{u}` `{active}` (measured 200 both directions).
  Measured at the pin: created users are `dormant` until first login (a boot
  lane only ever runs for logged-in owners, so springing never races that);
  deleted users are simply **absent from the default list** (`?status=deleted`
  does not filter them out — presence is the lane's test); the suspend route
  is 404 at this version, so suspend never disables (a suspended-but-present
  user keeps the account, which is the conservative choice).
- Degrades like every other identity note: any failure becomes
  `Skipped Gitea identity sync: …`, the pass stands, the next pass retries.

### Per-workspace LiteLLM identity: re-scope + churn fix
- MINTPY re-scoped to `<login>-<workspace>` (alias + derivation seed): Coder
  workspace names are unique only **per owner**, so the bare-workspace key let
  a second owner's "dev" adopt the first owner's key and spend. Metadata gains
  `coder_owner`; template tests pin the new shape.
- Churn bug found by the same recon: `reconcile_virtual_keys`'s retired-
  consumer sweep was deleting every workspace-minted key on every real
  shared_core pass (wizard metadata, alias not a state consumer → swept).
  Box-measured: `/key/list` held exactly the four state aliases after any pass,
  no workspace key — the boot lane re-minted at every restart, silent churn.
  The sweep now spares records carrying `coder_workspace`; pinned by a
  spare-the-workspace-key test.

- Wiring: `DokployCoderBackend` gains `gitea_provisioner`/`gitea_hostname`; the
  provisioner document is re-read at push time (the gitea phase reconciles
  before coder in the same process — identical to the FileX lane, own test).
  `_coder_request` accepts `session_token` (the measured `Coder-Session-Token`
  header; `CoderSessionToken` and `?session_token=` return 401 at the pin).
- MINTPY upgrade lane: the pre owner-scoping token (issuer + bare workspace)
  is deterministic too, so the boot retires it through `/key/delete` once the
  scoped key stands — otherwise the sweep's workspace-key spare strands the
  old-format key forever (nothing would ever mint or delete it again).

### Box-measured bugs (both fixed, both pinned by tests)
- **Gitea admin GET-by-name is not routed.** The first boot lane probed
  `GET /api/v1/admin/users/{name}` and boxed with `provisioner token rejected
  … (HTTP 405)`; a missing name 404s from the redirect middleware, so the code
  never had a working GET at all. Existence is now probed through the
  paginated collection `GET /api/v1/admin/users?page=N` (default page size
  30, capped pages) — mirrored in the backend's own sync list reader.
- **Adopt-back corrupted the LiteLLM state document.** The shared-core
  reconcile adopted `/key/list` records back into `litellm-generated-keys.json`
  whenever `record.key` differed from the state raw — but the live payload
  carries `key == token == sha256(raw)` with no raw material, so a no-drift
  pass replaced every consumer raw with its own hash. Irreversible; the
  validator stayed green (hash-vs-hash), and every later bake handed apps a
  token the gateway rejects with 401 ("LiteLLM Virtual Key expected.
  Received=b56b\*\*\*\*2a2d, expected to start with 'sk-'") — the workspace
  mint had silently fallen back to the (also hashed) baked key. The adopt-back
  now skips records whose `key` equals `token` (hashed list material proves
  nothing new); regression tests cover both the suppression and the genuine
  drift path still adopting its recreated raw.

### Box outcomes (157.173.124.231, modify30–modify32)
- modify30 (resume to the gitea prefix): sync lanes PROVEN — `Disabled Gitea
  account 'slice5-probe' (Coder user deleted)` and `Re-enabled Gitea account
  'slice5-return' (Coder user returned)` against seeded probes; the re-scoped
  alias `clayton-showmeseewhycom-workspace` appeared with `coder_owner`
  metadata. The same pass surfaced the 405 and the adopt-back corruption.
- State repair: fresh raws written into the box document (master/salt
  unchanged), stale scoped row swept by token, resume from the networking
  prefix.
- modify31 (resume through coder): value drift detected → all four consumer
  keys delete+recreated with the repaired raws; the state document stayed
  RAW through the pass (guard held on the box); `issuer GET /key/info?self`
  back to 200. Workspace boot lane (new container): `created the Gitea
  account for 'clayton'`, registered SSH key `clayton-showmeseewhycom-workspace`
  (visible in the admin list), wrote the `gitea` ssh_config alias + pinned
  `[showmeseewhy-com-gitea]:2222` known_hosts entry, set `user.name`/
  `user.email` git identity, and the scoped key re-minted hash-matching the
  newly derived token; the old bare-workspace format 404s (retired/never
  minted). Sync lanes idempotent: zero notes.
- Probe cleanup, then **modify32 PLAIN: `"mode": "noop"`, `"phases_to_run":
  []`** in 22s — the slice converges.
- Gates: 1116 passed, 1 skipped; ruff + mypy green. New tests: lazy gitea
  document read, sync scope matrix (manual/provisioner/present/deleted/
  re-enabled), scripted PATCH round-trip, patch-failure fail-closed, hook
  integration + degrade-to-note, GITEAPROV template contract (incl. the list
  probe), re-scoped MINTPY contract (incl. the upgrade cleanup), sweep spare,
  adopt-back suppression + drift-still-adopts.

## Phase 9 (2026-11): full-lifecycle clean-box proof (uninstall → cold slate → noop)

Destructive validation of the whole lifecycle on the test box, in order:
read-only drift report, destroy-data uninstall, from-zero install, resume
repairs, and a plain noop. It found and drove the fixes for two real bugs
(below), both committed with regression tests.

- **`upgrade --check` (read-only): clean.** Every component `status: ok`
  against its state pin; LiteLLM reports `pin_source: state-pin` at
  `v1.83.14-stable` with `bump_available: false` (already newest stable —
  nothing for the operator to approve right now).
- **Uninstall (destroy-data) — ledger behavior box-proven.** Deleted
  resources land in reverse-dependency order: gitea service + data after
  filex and *before* seaweedfs (the `gitea` bucket outlives the app, as
  designed), zone DNS records pruned, `state_cleared: true`, and the state
  shrinks to `[preflight, dokploy_bootstrap]` — Dokploy itself survives by
  design.
- **Box-measured runtime gap (backlog, NOT fixed):**
  `ShellUninstallBackend.delete` is a no-op for compose resource types
  (only tailscale/Access/DNS act), so destroy-data cleared the ledger,
  state, and DNS in ~1 s while every stack service kept *running*. The
  next `proof` correctly refused: the orphaned model-sync task container
  now classifies as `manual_collision` drift — the guard works, but the
  operator must wipe the runtime at the docker level (services first per
  the recovery recipe, incl. the bootstrap; `docker rm` alone lets swarm
  reschedule tasks). Also measured: ~15
  `dokploy-wizard-auth-probe-*` one-off task containers accumulate across
  runs and are never reaped.
- **Cold-slate install (`proof`, 610.7 s):** all packs deployed and
  verified (FileX admin login verified, bucket created). It surfaced two
  honest degraded notes that were real first-install bugs, both fixed:
  - **inspect-state corrupted the uninstall gate** (fixed): the snapshot
    writer overwrote canonical `raw-input.json`/`desired-state.json` with
    `<REDACTED>` report values, so the applied-vs-persisted fingerprint
    gate could never pass again — and every remote flow runs
    inspect-state, so uninstall was permanently disabled on every remotely
    managed box. The snapshot rides a dedicated
    `inspection-snapshot.json` now (cleared with state at teardown); the
    regression test replays the gate's exact comparison.
  - **Gitea provisioner seed raced the container** (fixed): the seed-gate
    reorder from Phase 8 held — see modify34 below.
- **modify33 (resume from the networking prefix):** the engine itself
  rewrote the canonical pair, and the uninstall gate passed on the box
  (applied fp == desired fp).
- **modify34 (resume from the filex prefix, fixed code):** gitea phase
  reports `initialized: true, provisioner_ready: true` with zero WARNINGs
  — the provisioner seeded cleanly against the fresh (post-wipe) database;
  the coder phase baked the door with the live token, updated the
  workspace onto the active template, and produced **no** `Skipped Gitea
  identity sync` note (the sync read the roster fine). Workspace boot (new
  container): `created the Gitea account for 'clayton'` on the fresh DB,
  registered `clayton-showmeseewhycom-workspace`, rewrote ssh_config +
  known_hosts + git identity; the scoped key re-minted with
  `hash-matches-derived=True` (issuer raw unchanged since the cold
  install → same derived token) and the old bare-workspace format stays
  404. The LiteLLM state document stayed RAW through the pass.
  Read-only gate probe on the box afterwards: fingerprints match,
  `desired-state.json` unredacted, snapshot in its own file — the
  inspect-state corruption cycle is closed.
- **modify35 PLAIN: `"mode": "noop"`, `"phases_to_run": []`** (23 s) —
  the full lifecycle converges on the box.
- **Uninstall dry-run through the real CLI path** (on-box, after the
  inspect-state pass): the fingerprint gate passes (it raised in
  uninstall1), the destroy plan orders gitea *before* seaweedfs with
  `remaining_completed_steps: [preflight, dokploy_bootstrap]`, and every
  state document md5 is byte-identical before/after — the gate is proven
  live, the corruption cycle closed end to end.
- Gates: 1118 passed, 1 skipped; ruff + mypy green.

## Phase 10 (2026-11): Gitea pack-form proof + per-user workspace-key policy

### Gitea PACKS conversion, box-proven (modify36)
- The `fcfa5f9` conversion (PACKS canonical, `ENABLE_GITEA` one-release alias)
  flipped the testing env from flag form to `PACKS=...,gitea` against the box
  whose applied state was installed with `ENABLE_GITEA=true`: modify36
  measured **`"mode": "noop"`** (23.8 s), reasons `Requested raw input and
  desired state match the persisted target.`, zero deprecation warnings on the
  PACKS path — alias equivalence confirmed live, not just in unit pins. The
  Slice 5 identity-automation box verify re-ran green on the same pass.

### Per-user LiteLLM workspace-key policy
- Operator knobs: `ALLOW_LITELLM_MODELS_FOR_NEW_USERS` (default false;
  `FALSE`/`false`/`0`/`no`/`off` all parse) and
  `ALLOWED_LITELLM_ADDITIONAL_USERS` (CSV of Coder logins; lowercased,
  trimmed, deduped, sorted at parse). Both land on `DesiredState` (sorted
  post_init check, `to_dict` always, `from_dict` optional-with-defaults so
  pre-policy on-box docs load byte-equivalently) and both join
  `_LITELLM_SHARED_CONFIG_ENV_KEYS`: a policy flip routes
  `shared_core` (reconcile sweep) + `coder` (template re-seed), and the first
  pass that merely *adds* a default-valued key reruns once (raw-based
  routing), then noops.
- Semantics: a workspace-minted key (template MINTPY lane) carries the
  operator's models only for owners the policy grants — flag true (everyone),
  allowlist entry, or the seeded admin login (always exempt, derived from
  `DOKPLOY_ADMIN_EMAIL` through the shared `identity.username_from_email`
  helper that the Coder first-user lane also uses). A denied owner still mints
  its personal key, scoped to the sentinel model
  `dokploy-wizard-model-access-denied`: boot stays healthy (the no-key guards
  stay quiet, the bootstrap re-default can't resurrect the baked key), spend
  stays attributed, and inference fails honestly at the gateway instead of
  leaking the shared baked key via the fallback.
- Enforcement is double-lane by design. Boot lane: MINTPY compares the
  existing mint's `/key/info` grant against the baked policy and, on mismatch
  (the issuer route set has no `/key/update`), retires and re-mints under the
  same deterministic token. Admin lane: `reconcile_virtual_keys` gained
  `workspace_key_policy`; the sweep re-derives each mint's token
  (sha256(issuer + ":" + owner + "-" + workspace)), addresses it with the
  master key, and `/key/update`s the grant — so a policy flip converges
  **without rebooting any workspace**. Owner-less mints are spared
  (conservative), operator-marked records are invisible, and the sweep is
  best-effort per key (failures wait for the next pass or boot).
- New box expectation: the policy keys' first landing reruns shared_core +
  coder once (sweep converges, template re-seeds with the four new
  `__DOKPLOY_WIZARD_LITELLM_{ALLOW_NEW_USERS,ALLOWED_USERS,ADMIN_LOGIN,DENY_MODEL}__`
  replacements); the seeded admin's existing mint must keep its grant
  untouched, and a second pass must classify noop.
- Gates: pytest + ruff + mypy green (policy parse/back-compat, routing, sweep
  degrade/upgrade/spare/idempotence, template bake + replacement-map pins).

### Coder-lane fix from an upgraded-box diagnosis (`3d045b6`)
- Diagnosing why a **different project's upgraded box** (pre-stable-naming
  wizard state, Coder v2.38.3) never adopted its legacy workspaces surfaced two
  real bugs, now fixed and pinned:
  - `_workspace_template_name` called `coder show --output json`, which that
    build rejects (`unknown flag`), so the migrate lane saw None for every
    workspace and retired-template deletes stayed blocked forever. The lookup
    now reads the same `coder list --output json` payload the rest of the
    module already parses (flat `owner_name`/`template_name`, nested
    `latest_build` fallback kept).
  - The bootstrap-ready branch skipped `_ensure_default_workspace` entirely —
    exactly the branch an upgraded healthy box takes — so adoption/pruning
    could never converge there. The adopt-or-create lane now rides every
    coder pass (read-only exists noop on converged stacks; the
    template-version convergence check it used to run directly lives inside).
- showmeseewhy box proof of the fix code: **modify39** landed
  `ALLOWED_LITELLM_ADDITIONAL_USERS=` (empty value: raw changes, desired state
  identical) → `mode: modify`, phases exactly `shared_core`+`coder`, zero
  WARNINGs, no adopt/prune noise on the converged box; **modify40** →
  `mode: noop` (19.8 s). Slice 5 re-verify after both passes: minted key
  present and hash-matches-derived, issuer raws RAW, Gitea identity lane and
  workspace ssh/git artifacts healthy.
- Deny-lane live proof (showmeseewhy, second Coder user `assistant` created via
  API + workspace `denyproof` on the default template):
  - Boot lane minted `assistant-denyproof` with models
    `["dokploy-wizard-model-access-denied"]` — **DENIED** verdict,
    token-hash-matches-derived, owner/workspace metadata present, boot stayed
    healthy (policy FALSE honestly denies at the gateway without breaking
    startup).
  - Allowlist flip to `assistant` → **modify41**: `mode: modify`, phases
    exactly `shared_core`+`coder`, 161.4 s, zero WARNINGs. Post-check: the mint
    re-scoped to GRANTED (`sync/*`,`sync/default-chat`) admin-side with
    **no workspace reboot** — `latest_build.id` byte-identical to the boot-time
    build — and the seeded admin's mint stayed GRANTED throughout (exemption).
  - Allowlist reverted to empty → **modify42** re-demoted the mint to the deny
    sentinel admin-side (again no reboot, no WARNINGs); **modify43** →
    `mode: noop` (24.4 s).
  - Cleanup: workspace deleted (owner-CLI), Coder user and gitea account
    removed, orphaned mint deleted via `/key/delete`; slice5 re-verify after
    cleanup fully green — box is back at exactly its steady-state roster.
  - Build-API quirks recorded for this Coder v2.38.x-class box: `/api/v2/
    templates` returns a bare array; admin create-user requires
    `organization_ids` + inline password with `login_type: "password"` (bypass
    rejected); delete-user is `DELETE /api/v2/users/{id}?convert_to_user=false`
    (legacy `?user_id=` route 405); `POST /api/v2/workspaces` 405 — workspaces
    are created through the in-container `coder create` CLI lane instead.

### From-zero redeploy on the operator env + seed-guard fix (`ffbbbc2`)
- The operator's login failure at `gitea.showmeseewhy.com`/`filex.` traced to
  two stacked causes: (1) the box had been driven for ~40 passes against
  `.install-min-testing.env`, so the applied admin password was the testing
  one, and (2) `.install-min.env` itself was mis-pointed — Cloudflare
  account/zone/token belonged to the **openmerge.me** project and
  `ROOT_DOMAIN=showmeseewhy` lacked the TLD, so a first fresh deploy failed
  its public-URL health wait against `*.dokploy.showmeseewhy`. Repointed the
  env trio to the showmeseewhy.com zone (verified via `/zones?name=` before
  and after) and removed the 7 stray `*.showmeseewhy.openmerge.me` CNAMEs the
  failed run left in the other project's zone (no stray tunnel).
- Wiped the box to true zero (uninstall destroy + manual swarm/compose orphan
  sweep, including the Dokploy bootstrap — a bootstrap keeps the admin
  credentials it was initialized with, so reusing it would keep the testing
  password forever) and ran `remote proof` on the corrected env: full green,
  all phases, verify pass, tailscale online as `showmeseewhy-1`, tuxdesktop
  local-model endpoint 200 through tailscale.
- The from-zero proof surfaced a real template bug (pinned + fixed): the
  FileX/Gitea seed guards listed the **mapped** marker
  `__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY__` inside a `case` pattern list, so
  after substitution the issuer compared equal to itself and the guard always
  fell to the provisioner-token seed — workspace-account passwords silently
  stopped following the documented issuer-derivation rule (and the
  provisioner token rotates; the mint lane's `!= sentinel` form was never
  affected, which is why workspace mints always hash-matched). Live proof:
  baked script showed the issuer inside its own pattern list; deleting the
  two stale accounts and rerunning modify (which pushed the fixed template
  and updated the workspace) re-sprung both with the issuer-derived password
  (gitea basic-auth 200, filex login 200, shared mount ready).
- `proof`-with-`--fresh` note: the destroy step issues Dokploy compose
  deletes asynchronously, so install can trip its own live-drift gate while
  swarm still tears down. On this box the task containers never drained;
  finishing the teardown manually (rm containers/volumes/networks) converged
  cleanly. A post-destroy settle/verify in the fresh flow is a candidate
  follow-up for the remote helper.
- Post-fix convergence pins: modify44 (`mode: modify`, shared_core+coder,
  template pushed, workspace updated onto the fixed version, zero WARNINGs)
  → modify45 (`mode: noop`, 26.9s). Workspace mint present and
  hash-matches-derived; seeded admin exempt (GRANTED) with
  `ALLOW_LITELLM_MODELS_FOR_NEW_USERS=FALSE`.
- Operator steer after the above: the gitea human account must sign in with
  the stack admin password, not a derivation. The GITEAPROV lane now bakes
  `__DOKPLOY_WIZARD_GITEA_ACCOUNT_PASSWORD__` (the same value the OpenCode
  server and Hermes webapp markers carry), creates the account with it, and
  re-syncs it through the admin API on every boot for wizard-owned accounts
  (email-convention guard preserved: manual accounts are never reset). The
  gitea half of the issuer-seed case block is gone with the derivation,
  leaving exactly one issuer `case` (FileX); the per-owner FileX account
  `~/shared` mounts with keeps the `fx-` derivation (machine credential).
  The owner's real Coder email is additionally registered as a verified
  secondary (box-measured on 28.4.0: `POST /api/v1/user/emails` with
  `{"emails": [..]}` → 201 `verified: true`, and basic auth with the email as
  identifier answers 200), so gitea accepts username or real email; the
  convention email stays primary because the admin user list only ever shows
  the primary and that is what the ownership guard reads. Box proof:
  modify46 pushed the template and the workspace boot logged "synced the
  Gitea password … to the stack admin password" (basic auth username + admin
  password → 200; real email + admin password → 200 after the email
  registration).
- Second from-zero proof on the operator env (post identity-lane commit): the
  cold rerun surfaced two more real bootstrap bugs, both fixed and pinned
  (`44dca64`, `6a47247`): (1) a host whose tailscaled is installed but stopped
  made `tailscale up` die with "failed to connect to local tailscaled" —
  the apply path now starts the unit itself; (2) Dokploy's first-boot
  migration seeds a passwordless placeholder (`nxploited@dokploy.com`) owning
  the default organization, and its sign-up endpoint refuses creators while
  that placeholder owns it — whether a from-zero install hits this is a race
  against the migration (cached images = healthy fast = lose), so the
  bootstrap now evicts the placeholder (scoped: email match AND no account
  row; member/account/session FKs cascade) right before authenticating.
  Related tailnet note: after `tailscale logout` + wiping `/var/lib/tailscale`
  the box re-registered under the clean `showmeseewhy` name (the earlier
  `showmeseewhy-1` suffix was Control-side name theft by a ghost device of a
  prior deploy generation). Final cold proof: every phase green, verify pass,
  436.4s, zero WARNINGs; the workspace boot lane itself logged creating the
  account, registering the real email, and the SSH key. Login matrix from
  zero: gitea by username AND by real email (admin password) 200, filex 200,
  coder 201, dokploy sign-in 200, docuseal serving; tailscale online as
  `showmeseewhy` with tuxDesktop peer and the local-model endpoint 200.
  modify47 converged to `mode: noop` (29.2s).

### Phase 10b (2026-11): the local endpoint became a real sync source

Operator request: stop swapping a `LITELLM_LOCAL_MODEL` value every time the
served model changes — expose whatever the endpoint serves, by its own name,
and let the autosync sidecar rescan it like any other provider. Delivered:

- `model_sync.py`: `"local"` joins `MODEL_SYNC_DEFAULT_PROVIDERS`; a
  `LITELLM_LOCAL_BASE_URL` alone appends a source whose `list_url` is the
  endpoint's own `/v1/models`. The source is NAMED for the serving host's
  first label (tuxdesktop.felis-aeolian.ts.net → `sync/tuxdesktop-<id>`),
  not a generic "local": the picker names the machine that answers, and two
  operators get two honest names. IPs keep their dotted form; a hostless
  URL falls back to `local`. Cap 50 (`MODEL_SYNC_LOCAL_MAX_MODELS`); model
  ids arrive in the OpenAI `data[].id` shape the sidecar already parses,
  so registration/alias-slug/prune/`_alias_source` worked untouched.
- Key handling: with `LITELLM_LOCAL_API_KEY` set the source carries
  `api_key_env` and the key now rides `_build_litellm_upstream_creds`
  (`local_api_key_env`) — one env line on BOTH the gateway and the sidecar
  (the dedicated local spec/compose lines collapsed into the generic
  provider rails). Without a key the source ships `default_api_key:
  sk-no-key-required` (new `deployment_params` support) because LiteLLM's
  OpenAI-compatible client refuses to build a request with no key; auth-
  skipping servers discard the placeholder — the same value the static
  lane has always used.
- Local sources declare `litellm_params_extra:
  {supports_system_message: false}` (new generic passthrough in
  `deployment_params`), mirroring the renderer's static-local posture for
  vLLM-family templates that reject non-leading system messages.
- env validation relaxed (`state/env.py`): the base URL alone is a complete
  lane; MODEL+KEY remain a pair (they pin the static
  `local-model.internal/unsloth-active` alias), dangling MODEL/KEY without a
  URL still error. Planner/renderer trio semantics untouched — the static
  alias still renders iff all three are set, and
  `_litellm_local_upstream_configured` keeps matching that exactly.
- Swap-in-the-picker story: consumer keys keep `sync/*`; the sidecar merges
  discovered names per cycle, and a swap on the serving box needs no wizard
  rerun at all — the next cycle (or the consumer-keys mtime wake) prunes
  the gone alias via `managed_by` tagging and registers the new one.
- Gates: 1143 passed / ruff / mypy clean; 8 new tests cover host naming,
  keyless listing + placeholder key, provider-subset respect, the
  disabled override, plan inclusion for local-only stacks, and a sidecar
  create/prune round trip asserting `sync/tuxdesktop-Qwen3.8-...` exactly.
- Box proof (showmeseewhy, modify48/49): the operator env commented its
  `LITELLM_LOCAL_MODEL` line; the modify pass applied (173s, "Supported modify
  keys changed: ['LITELLM_LOCAL_MODEL']") and the sidecar's boot cycle
  registered the discovered model live — `/model/info` shows
  `sync/tuxdesktop-Qwen3.8-Flash-Next-Uncensored` →
  `openai/Qwen3.8-Flash-Next-Uncensored` @
  `http://tuxdesktop.felis-aeolian.ts.net:30000/v1`, tagged
  `managed_by=dokploy-wizard-model-sync` `sync_source=tuxdesktop`,
  `supports_system_message: false` applied; the static
  `local-model.internal/unsloth-active` is gone with the pin; the sync
  service env carries `LITELLM_LOCAL_API_KEY` (creds rails cover it); the
  `coder-hermes` workspace key's `/v1/models` lists the alias by name, and a
  chat completion through it answered from the serving box. Second modify:
  `mode: noop` (21.4s).

### Phase 10c (2026-11): Gitea identity fix — the account IS the person

Operator request: retire the `coder.wizard.local` convention — every surface
of the stack should take the real login email, and the Gitea profile shown in
screenshots must stop reading like a machine placeholder. Delivered:

- Gitea lane: the admin-API create now takes the owner's REAL Coder email as
  the account email. Box-measured on 28.4.0: a create with a real email lands
  `verified: true, primary: true` instantly and basic-auth login by that
  email answers 200 — so the old "register the real email as a verified
  secondary, keep the convention primary" dance (lane step 1b) is gone, and
  the real email is itself the ownership marker. The guard flips accordingly:
  existing account with a DIFFERENT email is manual (skip, as before); one
  carrying the legacy `{login}@coder.wizard.local` primary is a pre-fix
  spring and gets `PATCH /admin/users {email}` (200 moves the primary,
  measured; 409 means the email is already registered — on pre-fix boxes it
  sits on the same account as a verified secondary that logs in fine, and
  this build has no owner per-email routes at all: GET/DELETE
  `/api/v1/user/emails/{email}` and every make-primary spelling 404, all
  measured).
- Ownership guard reroot: the coder-phase sync no longer scopes on the email
  convention. It claims a Gitea row when username AND primary email match a
  live Coder user and persists the claim set to `gitea-identities.json`
  (0600, `GiteaIdentityLedger`). The ledger is REQUIRED, not stylistic:
  deleted Coder users are unqueryable (measured: `q=probeuser`,
  `status:disabled`, `type:system` all come back empty on v2.39.4), so
  without durable memory the sync could not disable the accounts it sprung —
  and with real emails everywhere it can no longer tell ours from manual by
  inspection either. Enable/disable semantics unchanged: tracked ∧ absent →
  disable; tracked ∧ present ∧ inactive → enable.
- FileX lane: the mount account is the person, not a twin. Lookup is by real
  email first (the deployer's IS an account — the pack's admin bootstrap —
  and the mount rides it; box-shape: admin id 1 real email, provisioner id 2,
  the pre-fix lane twin id 3 carried the convention email), create-with-real-
  email when missing, manual-name-under-other-email skips. The derived
  `fx-`-sha256 seed is retired: the lane re-PATCHes the adopted account's
  password to the stack admin password (`PATCH /api/admin/users/{id}` →
  200 `{"ok":true}` measured) and signs in with it — one operator credential
  on this surface too. The issuer-key seed case in the startup script died
  with it (the workspace-key mint guard is the only issuer consumer left).
- Root-cause note for the record: the "401 on the derived password" mystery
  was two accounts — probes had locked the ADMIN account (real email) while
  the derivation belonged to the lane twin; FileX locks an identifier after
  repeated failures (`"too many attempts","locked":true`, measured).
- Gates: unit pins updated for the ledger signatures, the new template text,
  and the replacement map (FileX account password key added); issuer-guard
  regression test updated — zero `case`-form issuer guards now survive, the
  FileX seed was the last.

## Phase 11 (2026-11): screenshot studio, ledger convergence, demo teardown (box: 157.173.124.231)

### Studio (README gallery, deterministic redaction)

- `screenshots/studio/` (Playwright capture → sharp blur/caption redact, docker
  image `dokploy-wizard-screenshot-studio:local`): 39 shots across
  dokploy/coder/gitea/filex/litellm/docuseal/walkthrough/shared/tailnet/day2,
  captured with deploy-env credentials, fail-closed on anonymous landings
  (per-service `loginPaths` guard incl. array/exact-match support for Dokploy
  v0.31.4, whose sign-in form is served at `/` while `/login` 404s; DocuSeal
  login measured at `/sign_in` — `/account/sign_in` renders the app's own 404).
- Auth model: fresh browser context per shot (saved storage states poisoned
  login shots and LiteLLM deep links); LiteLLM session gate =
  waitForResponse(/user\/info/) + landing `table tbody tr`;
  `waitForSelector` honored before `wait`; one automatic retry per shot.
- Redaction: dotless (`showmeseewhycom`) + hyphenated (`showmeseewhy-com`) +
  input-value variants, blur of root domain / `clayton` / `superiorbyteworks`
  / `tuxdesktop` / `felis-aeolian` / box IP; `dokploy-wizard` provisioner stays
  visible; captions spell `«your-domain»`; finals committed via Git LFS
  (`.gitattributes` tracks `screenshots/**/*.png`), raw + `.meta.json`
  gitignored, `redact.env` gitignored 0600.
- Redaction pass: 39 images, 69 regions (dokploy-projects alone: 18).
  **Vision review: all 39 finals accepted** (no Nextcloud UI shots; hermes
  tile-only).

### `modify --force-phase <phase>` (this branch)

The Gitea identity ledger (`gitea-identities.json`) is written inside the
coder apply, which a no-op modify skips. Added `--force-phase` to
`dokploy-wizard modify` (and forwarded by the remote helper) on top of the
existing `force_phase_into_plan` — folds one already-completed phase into the
plan for one real pass; honest per-caller reason (modify wording vs the
upgrade's shared-core wording). Five tests (parser, handler pass-through,
operator-reason build, explicit-reason preservation, remote command builder).
Full gates: **1150 passed, ruff clean, mypy clean**.

### Ledger convergence + demo teardown (box)

- Forced coder pass #1 (46.2s, mode modify): template re-pushed,
  `/root/dokploy-wizard/state/gitea-identities.json` written —
  `{"format_version":1,"usernames":["clayton","demo"]}` (0600).
- Demo teardown: v2.39.4 workspace DELETE is CLI/transition-based (API
  `DELETE /api/v2/workspaces/{id}` → 405; first delete-build errored on
  `home_volume` 30s removal poll — container + volume verified gone anyway).
  In-container `coder delete --yes <id>` retry: **deleted**. User then
  `DELETE /api/v2/users/{id}?convert_to_user=false` → 200; roster: clayton.
- Forced coder pass #2 (45.5s): `"Disabled Gitea account 'demo' (Coder user
  deleted)."` — the claim persists in the ledger (by design; Gitea accounts
  are disabled, never deleted).
- Final plain `modify` (22.8s): `"mode": "noop"` — box fully converged.

## Phase 12 (2026-11): one-ID identity, Gitea privacy default, FileX editing sidecar, Nextcloud retirement (box: 157.173.124.231)

Twelve commits (`3929678` → `9f701e8`), 85 files, +3,843/−8,070. The wave the
branch name hinted at and then some: every machine identity the box carried
was either folded into a person's own account or retired, Gitea closed its
anonymous web, the file story consolidated onto FileX with in-browser
editing, and Nextcloud retired with its sidecar.

### One-ID identity (3929678, ebca77c, 561a7d1)

- LiteLLM: a person IS one user. Workspace keys carry the owner's `user_id`
  and ride the alias pattern `coder-<login>-<workspace>`; model access is
  granted through the named group `all-access` (`WORKSPACE_KEY_ACCESS_GROUP`)
  instead of per-key model lists. The deployer is the box's only
  `proxy_admin`; every other person is `internal_user` (spend/log views
  scoped, own-key management). The retired per-workspace user rows clean up
  through the identity lane.
- The coder apply became the fan-out point: one-ID sync pre-stands the
  Gitea, FileX, and gateway (SeaweedFS) user rows from the live Coder roster
  before any boot lane needs them, so a first workspace boot never races an
  account creation.
- Both service accounts died: the Gitea `dokploy-wizard` bot and the FileX
  `wizard-provisioner`. The deployer's own account IS the provisioner
  (login derived from the admin email, password CLI-pinned every pass); the
  FileX lane token is an admin-scope token on the MAIN user labeled
  `workspace-provisioner`. Retirement is gated on the replacement token
  proving alive; an account still owning repositories survives with a
  visible warning.

### Gitea private by default + boot-lane seed (561a7d1, 056bed7)

- `DEFAULT_USER_VISIBILITY: private`, `DEFAULT_PRIVATE: 'true'`,
  `DISABLE_ANONYMOUS_ACCESS: 'true'` — opt-in public survives (an owner's
  explicit choice is never re-closed), but nothing leaks before the owner
  chooses. The coder pass flips wizard-owned accounts that predate the
  default to private, once, logged; manual rows are never touched.
- The workspace boot lane seeds each person with a private `wizard-demo`
  repository — adopt-or-create: a repo that already stands rides untouched
  (even a public one), and the seed push lands only in an empty repo, over
  the workspace's own SSH key. Provenance matters for the reshoot: the seed
  exists only through the boot lane, so its README commit carries the person
  who booted, not a bot.

### Nextcloud retirement (1ee15ce)

- Pack removed, ledger authority kept: nextcloud-era rows (DNS, Access apps,
  compose stacks, volumes) still drain on uninstall like every retired
  resource. The uninstall planner's `removed_hostnames` set subtracts
  hostnames a requested pack still owns, so the ONLYOFFICE key riding the
  filex pack does not get its DNS/Access rows torn by the retirement.

### FileX + ONLYOFFICE editing sidecar (446b522, ca1abb3)

- Shipped as a pack-owned sidecar, digest-pinned
  (`onlyoffice/documentserver:9.4.0.1@sha256:3ab6ebc…`), JWT enforced on
  every hop with a deterministic per-stack secret
  (`sha256("dokploy-wizard:filex-office:{stack}:{stack}-filex-onlyoffice-jwt-secret")`),
  recorded at `filex-office-jwt.json` (0600); editing round-trips land as
  ordinary versions in the same versioned bucket.
- **The same-origin pivot (measured).** The first design mounted the sidecar
  at `filex.<root-domain>/filex-frame` with a runtime prefix-pin. Two
  box-measured facts killed it: FileX refuses an editor frame on its own
  origin (config guard crash-loop), and docservice does not serve its
  statics under `relative_url` at all. The fix removed 238 lines of pin
  machinery: the sidecar got its own root-mounted hostname,
  `office.<root-domain>` (env key `ONLYOFFICE_SUBDOMAIN`, reusing the
  retired nextcloud naming), no stripsuffix, no supervisorctl.
- **DS boot timeouts (measured).** DocumentServer's entrypoint runs a
  migration storm on first boot; a gigabyte-class cold pull precedes it. The
  pass gates in the order the box teaches: container-up first (180 attempts),
  then `/healthcheck` behind the edge (48×10s), then the JWT record — and it
  records nothing until the gate passes.

### Classifier and drift-gate lessons (1bbcab3, a524be6)

- The modify hostname-change classifier now derives hostname→phases from the
  pack catalog (`_hostname_owner_phases`), never a hand-maintained table: a
  key that joins, moves, or vanishes between states always re-runs
  networking + the owning pack. The live box measured the rot — the editing
  sidecar joined filex under the old `onlyoffice` key and the old table saw
  no phase to run. `_LEGACY_HOSTNAME_PHASES` keeps only `dokploy` and `s3`.
- The drift gate learned that a pack's own runtime is drifted BY DESIGN while
  it is being scheduled: the office sidecar declares itself a managed
  auxiliary of the filex candidate, and `wizard_managed` unhealthy entries do
  not block when their own pack's phase is in `phases_to_run`. Manual
  collisions and host-local routes still always block.

### The upgrade-box repair: legacy-token bridge (915a5b8)

Box-measured: the previous generation left the deployer's Gitea account
non-admin AND carrying `must_change_password`, and Gitea 403s EVERY
basic-auth call while the flag is set — the CLI password pin does not clear
the flag (measured twice), so the token mint died and the converge carried
one WARNING. The only credential able to fix the row at boot time is the
legacy bot's own token (still admin until the retire step deletes it): one
drift-gated `PATCH /api/v1/admin/users/{login}` clears both columns and the
mint lands (200/200/201, measured live). From-zero boxes never need the
bridge — the CLI create seeds `--admin --must-change-password=false`.

### Studio rework + README (f56cc0d, 9f701e8)

- Playbook rebuilt for the one-ID world and office editing: 43 shots, new
  `filex-office-edit` / `filex-office-versions` / `gitea-logged-out` /
  `litellm-internal-user` recipes, no Nextcloud shots, fail-closed redaction
  unchanged (`REDACT_KEEP=dokploy-wizard,wizard-provisioner`).
- README rebuilt around the shipped story (pack list, FileX sidecar section
  with the measured gate order, Gitea privacy default + seed prose, one-ID
  identity lanes, office in both not-behind-Access lists, ONLYOFFICE login
  matrix row, roadmap bullet now saying shipped).

### The zero-state gate (operator directive)

Before the full studio reshoot, the deployment must land **from zero to
ready on a first pass with no fixes** — a one-time gate for this wave, not a
standing rule. Execution: remote `uninstall --destroy-data` (new per-env
fixture `fixtures/destroy.showmeseewhy-com.confirm` — line 3 must carry the
environment identifier), then a deliberate bootstrap wipe (services, task
containers, dokploy volumes, `/opt/dokploy`, `/etc/dokploy`). Docker image
layers intentionally retained — runtime material, not deployment state.
Then install, and loop zero→ready until the first pass is clean; only then
the reshoot.

Loop iteration 1 failed at 339s in the tailscale phase:
`Refusing to adopt existing unowned Tailscale node 'showmeseewhy'` — and the
guard was **right**. Uninstall deletes the tailscale ledger row retain-safe
(`tailscale down`, never logout: tailnet membership is operator state), so a
logged-in daemon survives teardown; the reinstall found an authenticated node
the fresh state document did not own and refused to hijack it. A logged-in
tailscaled is not zero state: the wipe recipe now ends with
`tailscale logout` (loop iteration 2 chained uninstall → wipe-v2 → install
under way). The fail-closed behavior stays — the wizard never claims
machines it did not mint.

Loop iteration 2 reached the end of the first pass (535s) but not the bar:
the gitea lane logged `WARNING: Gitea provisioner API token could not be
minted`, and the coder phase skipped both identity syncs at HTTP 401. Two
wizard bugs, both caught by the loop existing to catch them, both nailed
shut with controlled on-box experiments rather than theories:

- **The password pin re-stamped the must-change flag** (`3fad887`). The
  box ran the experiment: create WITH `--must-change-password=false` → row
  false (the wizard's create argv was always right); a flagless
  `change-password` (the pin's shape) → row TRUE again; the same pin with
  the explicit false → row false, basic auth back to 200. This Gitea build
  defaults the flag to TRUE on change-password — so the pin undid the
  create every pass, and the mint died at 403 one step later. Same stamp,
  same 403, as the upgrade box's flagged `clayton` years earlier: one root
  cause, both lanes. The legacy bridge stays for upgrade paths (the hat is
  not the pin's job).
- **Full teardown spared every pack document** (`143401f`).
  `STATE_DOCUMENT_FILES` is the fingerprint inventory (four core docs),
  and `clear_state_documents` treated it as the teardown list — so
  `state_cleared: true` unlinked gitea-provisioner.json and friends into
  a fresh install's path. The box proved it with mtimes: a doc written
  hours before the uninstall, read by the install minutes after. New
  `PACK_STATE_DOCUMENT_FILES` (exported, consumed only by the clear path);
  the fingerprint tuple untouched, so the install/modify/inspect set
  semantics hold. The stale provisioner doc is what burned the token
  verification, and the identity syncs' 401s rode it.

Also measured during the same diagnosis: Dokploy's stack removal does not
delete named volumes — pre-fix generations of `showmeseewhy-com-*` data
volumes stood on the box by the hour. Loop iteration 3's wipe therefore
takes `^(dokploy|showmeseewhy)` volumes along: true zero, nothing left to
secretly reuse.

Loop iteration 3 passed the bar: uninstall → wipe-v3 → install, first pass
581s, output grep-clean — zero WARNING/Skipped/Traceback, `provisioner_ready:
true` with the mint note, both identity syncs ran, the ONLYOFFICE gate logged
its ready note, tailscale reported `create`. Box verify afterward: fresh state
docs only, privacy flags live in app.ini, exactly one Gitea user (the
deployer, admin, flag clear) with one token, the office sidecar answering
`/healthcheck` over the public edge, coder workspace running. The
boot-lane seed proved itself independently: first commit `386e2e08`
"wizard: private demo seed" authored by the person, repo private, non-empty,
`main`. One real defect surfaced in the verify, and the probes caught it red-
handed — see below.

Iteration-3 verify lesson, fixed in `14359fa`: the deployer's Gitea row
booted PUBLIC and the visibility migration said nothing, because the lane
rode `GET /api/v1/admin/users/{login}` — and on this Gitea 28.4 build that
route does not exist (405 with or without trailing slash, measured). The
admin CLI create does not consult `DEFAULT_USER_VISIBILITY` either, so the
fresh row arrives public and the dead pre-read made the flip silently skip.
A controlled on-box experiment mapped the alive surface: the paged admin
LIST answers with per-row `login`/`username`/`is_admin`/`visibility` but
omits `must_change_password`; the PATCH lane works (200, DB visibility
0→2). All three lanes that rode the dead GET now ride the list — the
migration (and a failed list read now logs a skip note: silence is what hid
this bug), the legacy bridge (its gate becomes the mint refusal itself — a
basic-auth 403 is the only must-change signal this build still gives, and
the PATCH is idempotent so a healthy account never feels it), and the
retire gate (keys off list presence; an unreadable list reads as absent and
the next pass retries). The same verify corrected a studio assumption:
a logged-out `/explore` renders a closed listing with NO sign-in form, so
the `gitea-logged-out` shot photographs the plain 404 a private repo gives
the anonymous request — the privacy proof that needs no interpretation.

Gate standing: iteration 4 (uninstall → wipe-v3 → install) must land a
first pass that carries the visibility-flip note for the deployer's row and
shows DB visibility 2 — the iteration-3 box already corrected its row and
can no longer show the note. The identities ledger being absent on a
deployer-only box is BY DESIGN: the ledger springs with the second tracked
person, and the deployer's seeded account needs no claim.

Iteration 4 failed the bar, and the loop caught a fifth real leak doing it.
The gitea phase raised `Gitea health check failed` — and the deployment log
inside Dokploy said why: `failed to create network …_default: all
predefined address pools have been fully subnetted`. The network inventory
explained everything: 35 networks on the box, ~31 of them stale
compose-project generations (ten auth-probe projects, three-to-five
generations per pack). Dokploy's stack teardown removes services, spares
volumes (the iteration-2 lesson) — and spares the project's `_default`
network; every uninstall cycle leaked another daemon allocation until the
finite default address pool finally drained. Fixed at the wizard layer
(`80dd116`): uninstall now sweeps docker networks and (in destroy mode)
volumes whose names carry the stack prefix — ownership by name, best-effort,
never fails a teardown, and the reclaim counts land in the audit payload.
The wipe recipe gains the same sweep plus a daemon restart (leaked
allocations inside the running daemon's IPAM only free on restart) and a
pool canary. Iteration 5 (next) must land the fully clean first pass the
gate asks for — flip note included.
