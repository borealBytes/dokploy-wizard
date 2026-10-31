# dokploy-wizard — PR A (Hermes Desktop webapp) + follow-on PRs B and C

Plan of record, captured 2026-10-30 so the research survives the session.
During implementation this document lands in the repository as
`docs/plans/hermes-desktop-webapp-and-follow-ons.md` as part of **PR A**, which is
what makes it durable rather than a chat transcript.

Base for all three branches: `origin/main` at `3275c73` (PR #12 added
`.install-min.env.example` and rewrote `core/planner.py`,
`litellm/config_renderer.py`, `state/env.py` — the autosync files PR A touches).

---

## 0. Ground rules (user-imposed)

| Rule | How it is honored |
|---|---|
| New feature worktree, nothing on `main` | `git -C dokploy-wizard fetch` then `git worktree add ../feature/hermes-desktop-webapp -b feature/hermes-desktop-webapp origin/main`, matching the existing `dokploy-wizard-worktrees/feature/<name>` convention; session working directory moves into it |
| Zero Docker work on the local machine | local checks are `pytest -q && ruff check . && mypy .`; anything that runs containers is remote |
| Every install/modify/upgrade test goes to the new Contabo VPS | `./bin/dokploy-wizard-remote <cmd> --host 203.0.113.10 --password … --env-file .install-min-testing.env` |
| Cloudflare blast radius | only zone `11111111111111111111111111111111` (`example-rootsite.com`) in account `22222222222222222222222222222222`; the account holds other zones that must not be touched |
| README | surgical edits to specific sections, no rewrite |
| No `git push` / PR creation without being asked | `AGENTS.md` rule |
| Never edit `~/projects/dokploy-wizard/.install-min.env` | tell the user what to add instead |

---

## PR A — `feature/hermes-desktop-webapp`

Goal: a new Coder template `ubuntu-vscode-hermes-desktop` that runs the
official Hermes Desktop **webapp** as the primary chat tile, plus the three
production fixes from the handoff, plus pattern-based model defaults.

### A1. Evidence gathered (drives the design)

| Probe / read | Result |
|---|---|
| Official web surfaces | `hermes dashboard` (port 9119) = admin panel with a TUI-over-PTY chat tab. `hermes webapp` = the **full Desktop workspace in a browser** (sessions, profiles, Capabilities, Messaging, Artifacts, cron, host file tree, Git review, previews, terminal rail), served **by the gateway** on port **9120**. Config: `webapp.enabled/host/port/public_url`, read at gateway start. Build: `hermes webapp --build-only` (npm deps land in `.build/webapp-workspace`) |
| Auth rule | Loopback + no provider = **one-use link**, valid 60 s, single use → unusable as a tile. `webapp.host: 0.0.0.0` **or** any `public_url` engages the sign-in gate, which is **fail closed**: with no `DashboardAuthProvider` the gateway serves native clients only and 9120 never answers. Bundled `basic` provider = `dashboard.basic_auth` (`username` + `password_hash`/`password` + `secret` + `session_ttl_seconds`, default 12 h), env overrides `HERMES_DASHBOARD_BASIC_AUTH_*` |
| Host pinning | `public_url` is "the one outside hostname the Webapp accepts requests for"; `hermes webapp --status` reports refusals |
| Plain HTTP remote | loses bot-screen viewer, microphone, clipboard read, notifications. Over Coder+Cloudflare the browser sees HTTPS, but a cross-origin iframe loses them anyway → `open_in = "new-tab"` |
| Coder subdomain routing | `coder.py:804` Traefik rule is `^[a-z0-9-]+(?:--[a-z0-9-]+){2,}\.<suffix>$` → Coder's `<app>--<ws>--<user>` form is **one label**, so the default `*.example-rootsite.com` wildcard (Universal SSL + one wildcard record) covers it. The historical "subdomains never worked" was the *nested* `*.coder.<root>` form, which needs Advanced Certificate Manager (`networking/planner.py:857+`) |
| Proxy 503 root cause | `/tmp/coder-mounted-proxy.mjs` (template heredoc ~`:811-884`) has **no** `res.on("close")`/`req.on("aborted")`. Cancelled SSE streams keep streaming into a dead pipe forever, which pins threads in the community WebUI's `QuietThreadPool` (`_MAX_WORKERS = 144`) until `QuietHTTPServer` serves the static `_OVERFLOW_RESPONSE` 503 with `Connection: close` — exactly the observed "restart the server and it works". Second-order bug: `/health` shares that pool, so Coder's healthcheck cannot tell "sick" from "full" |
| PATH gap | installer puts `hermes` in `~/.local/bin` (`NPM_CONFIG_PREFIX=/home/coder/.local`); template exports PATH only inside startup scripts (`main.tf:119,133,898,939`). `.bashrc`/`.profile` get pnpm's PATH (`:103-106`) but never `~/.local/bin`. Ubuntu's default `~/bin` block is itself `command -v`-guarded, so a `~/bin` symlink does not help |
| OpenCode Go catalog | 28 models; `deepseek-v4.1-flash` present (first entry). Identical list with/without `x-opencode-session` |
| OpenCode Go liveness | 24/28 answered a completion. The 4 that failed (`gpt-5.6-luna`, `gpt-6-luna`, `muse-spark-1.2/1.3-contributor`) failed only `max_output_tokens must be >= 16` → a 1-token probe yields **false negatives** |
| OpenRouter liveness | 456 models, 16 `:free`. 8 sampled: 3 broken — `recursive/flint-v1.0:free` 503 "no upstreams available", `xingchen-agi/xing4.0-29b-a4b:free` 429, `thinkingmachines/inkling-small:free` 403 "only available on agentic harnesses". Permanent (403/404) vs transient (429/503) are different classes of problem |
| Sidecar today | `litellm-model-sync` already scans live lists, registers/prunes `sync/<provider>/<model>` deployments by `managed_by`, liveness-probes (NVIDIA only, verdicts cached `MODEL_SYNC_DEFAULT_PROBE_TTL_DAYS = 7`), rewrites every consumer key's explicit allowlist each cycle (`/v1/models` does not expand `sync/*`), writes markers, has a healthcheck and an on-demand trigger |

### A2. Decisions

| Topic | Decision |
|---|---|
| Official surface | Gateway-served **webapp** (9120). `hermes dashboard` (9119) is **not** shipped as a tile |
| Tile set | `Hermes Desktop` (official, primary, `subdomain = true`, `open_in = "new-tab"`) + `Hermes WebUI (Community)` below it as the safety net |
| Tile naming | official slug `hermes-desktop`; community slug/icon unchanged, display name `Hermes WebUI (Community)`, order 4 |
| Sign-in | bundled username/password provider seeded by the template: username `hermes`, scrypt hash of the **stack admin password** (same source as the existing `__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__`), explicit `secret` so sessions survive restarts. Blank admin password never ships an open webapp |
| Bind | `webapp.host: 0.0.0.0` inside the workspace container + `webapp.public_url` computed in-workspace from a new `__DOKPLOY_WIZARD_CODER_WILDCARD_SUFFIX__` token and `data.coder_workspace.*`; fallback if Host pinning misbehaves is `0.0.0.0` with no `public_url` (verify empirically) |
| Template rollout | seed now as a required-but-optional template (soft-failure push), flip the default only after a real deploy proves it |
| Existing workspaces | stay on `-pi` (Coder binds workspace→template at creation); recreation is the migration path |
| Default model | pattern-based, resolved by the **existing** sidecar (no new container) |
| Scope of upgrade work | full stack-upgrade command lands in this cycle as **PR B** |
| Scope of removal work | one single PR (**PR C**) removing all five packs, code-only |

### A3. Commit plan

| # | Commit | Detail |
|---|---|---|
| 0 | (no commit) | worktree + branch off `origin/main`; session moves into it |
| 1 | `fix(coder): tear down proxy upstream when the client disconnects` | `res.on("close")` + `req.on("aborted")` → destroy `upstream`/`upstreamRes` and pair sockets on the upgrade path. **Not** `req.on("close")` — Node 18+ fires that on normal body completion. Applied to `-pi` and the new clone |
| 2 | `fix(coder): put hermes on the system PATH` | idempotent `~/.local/bin` lines in `.bashrc` **and** `.profile` (pnpm's idiom), plus `ln -sf ~/.local/bin/hermes /usr/local/bin/hermes` mirroring the opencode symlink at `:73` |
| 3 | `feat(coder): add the Hermes WebUI watchdog` | `~/.local/bin/hermes-webui-watchdog` (curl `/health`, restart, log to `/tmp/hermes-webui-watchdog.log`), registered idempotently in Hermes' 2-minute cron |
| 4 | `feat(coder): add the ubuntu-vscode-hermes-desktop template` | clone of `-pi`; `_default_hermes_desktop_template_dir/_name()` accessors, added to `_required_template_names()`, replacement-token tuple beside `-pi` at `coder.py:260`, new wildcard-suffix token |
| 5 | `feat(coder): serve the Hermes Desktop webapp with basic-auth sign-in` | `webapp.*` config, `dashboard.basic_auth`, `hermes webapp --build-only` in the background bootstrap script (never the Terraform path), gateway restart, `webapp --status` logged, both tiles |
| 6 | `feat(litellm): select the default model by pattern in the model-sync sidecar` | `AI_DEFAULT_MODEL_PATTERN` (default `deepseek*flash*`), version-tuple ranking (`v4.10 > v4.1 > v4`, no string compare) then upstream recency then catalog order; `sync/default-chat` promoted only after a real completion probe; promote-then-retire with its **own** rule because `sync_once`'s prune loop keys on `sync/<source>/…` segments and would drop a single-segment name during a total source outage; boot-time static route kept until the alias is confirmed; every consumer key allowlists it |
| 7 | `fix(litellm): stop exposing models that cannot answer` | probe floor `max_tokens >= 16`; min-token/context 400s = inconclusive, not dead; permanent (403/404/unsupported) drops the alias, transient (429/503/timeout) keeps and re-probes; extend probing to the OpenRouter ranked/free slice; keep OpenCode Go's honest list plus a chat-modality check |
| 8 | `feat(templates): resolve the workspace default from the live catalog` | `model-refresh.py` glob-ranks `/v1/models` to pin a concrete name for display in Hermes/OpenCode pickers; `sync/default-chat` stays the routing default |
| 9 | `chore(models): examples, pricing map, docs` | `.install-min.env.example` + the 3 other examples, `litellm/nvidia_model_settings.json` entry for the new default, README: new template table row, both Hermes surfaces, the 503/PATH fixes, `-pi` row |
| 10 | after a real deploy | `feat(coder): make Hermes Desktop the default workspace template` (`coder.py:1047,1052`) |

### A4. Model-default sites that must move together

| Site | Today |
|---|---|
| `_DEFAULT_LITELLM_ALIAS_ORDER` (`core/planner.py:24`) | `opencode-go/deepseek-v4-flash` |
| `_DEFAULT_AI_DEFAULT_MODEL` (`dokploy/coder.py:74`) | `deepseek-v4-flash` |
| `_DEFAULT_OPENCODE_GO_MODEL` (`litellm/config_renderer.py`) | same family |
| `.install-min.env.example` (new in PR #12) | `AI_DEFAULT_MODEL=opencode-go/deepseek-v4-flash` |
| `.install.env.example`, `.install-my-farm-advisor{,-min}.env.example` | `deepseek/deepseek-v4-flash:free` (openrouter) |
| `openclaw.py:121-122` | **vanishes in PR C** — whichever branch lands second must re-grep so the old name cannot be resurrected from a fixture |

`_opencode_go_static_model_ids` does `model.removeprefix("opencode-go/")` and the
catalog turns the result into `opencode-go/<model>`. So `AI_DEFAULT_MODEL` must be
the **bare** model id (or the `opencode-go/` form), never `sync/opencode-go/…`,
which would produce a double-prefixed route and leave the workspace default
unusable until the sidecar's first cycle.

### A5. Watch-outs

| Watch-out | Mitigation |
|---|---|
| A 1-token probe kills 4 healthy models (min output-token floors) | floor at 16, treat min-token 400s as inconclusive |
| OpenRouter free capacity shifts hourly | permanent/transient split + existing 7-day probe cache with dead models re-probed each cycle |
| The default alias must exist before the first sync cycle | keep today's literal boot route until `sync/default-chat` is confirmed, then it becomes the only one |
| Consumer key allowlists are explicit, not wildcards | a new alias is invisible to Hermes until its key allows it — add it in the same commit as the alias, plus a test that every consumer key allows the default |
| `public_url` pins the accepted hostname | verify with `hermes webapp --status` and `curl -H 'Host: …' 127.0.0.1:9120/`; fallback is `0.0.0.0` without `public_url` |
| Gated webapp fails closed | write the basic-auth trio unconditionally; log `webapp --status` |
| Empty `basic_auth.secret` invalidates sessions on every restart | explicit secret in `~/.hermes/.env` |
| First bundle build needs npm and minutes | background bootstrap script, generous `healthcheck.threshold`, community tile as safety net |
| Missing `*.<root>` wildcard is **silent** (`networking/planner.py:790-800` treats it as degradable) | one `curl -I https://<app>--<ws>--<user>.<root>` before flipping the default; `CODER_WILDCARD_SUBDOMAIN` is the knob |
| Default flip changes blast radius | default-template push failure is fatal to bootstrap, optional is soft (`coder.py:376`) |
| Hermes and the community-webui checkouts are reset/synced every boot | fixes live in the template, never in a checkout; C1/C3 also go upstream |
| `hermes` install is `--skip-setup` from `main` | upstream drift can rename or move `webapp`; gate on `command -v hermes` + `hermes webapp --status` and log clearly |
| Remote runs take 10-60 min | pass a ≥3,600,000 ms timeout or run in background; do not cancel a healthy install |

### A6. Test matrix

| Scenario | Command | Proves |
|---|---|---|
| Local gates | `uv run --all-extras pytest -q && uv run --all-extras ruff check . && uv run --all-extras mypy .` | `AGENTS.md` gate before commit |
| Fresh install | `./bin/dokploy-wizard-remote proof … --env-file .install-min-testing.env` | all templates seed, webapp builds and answers on 9120, sign-in works, tiles open, default model resolves |
| Modify, healthy box | `…-remote modify …` | new template lands, **no** second default workspace (regression guard), no pack regressions |
| Repeated modify | same twice | idempotent, no churn |
| Upgrade | `…-remote upgrade` (PR B) | drift report, gated redeploy, rollback |
| 503 repro | hold a `/health` connection and cancel it repeatedly (16 streams) | sockets die in 1-2 s, WebUI never enters overflow |
| PATH | plain `docker exec -it` terminal: `command -v hermes` | system PATH fix |
| Model selection | sidecar logs + `GET /v1/models` with a consumer key + `GET /key` allowlists | no broken aliases, default resolves and answers |

### A3b. Measured on the test box (drove fixes not in the original plan)

| Finding | Evidence | Action |
|---|---|---|
| Traefik answers **404 for any hostname it has no rule for**, which is indistinguishable from a dead service | `Host(litellm.example-rootsite.com)` → 404 while dokploy/coder → 200; the LiteLLM service was already on `dokploy-network` with an alias, only the router was missing | Compose renders the router + `X-Forwarded-Proto/Port` middleware for the admin hostname; the hostname also gets its own DNS record and tunnel ingress rule instead of riding on Coder's wildcard |
| Cloudflare ships every account with **one unnamed `onetimepin` provider** that cannot be deleted | `GET /accounts/<id>/access/identity_providers` → `{"id":"c27b128b-…","name":"","type":"onetimepin"}` | Reading a nameless provider is valid; finding by name falls back to it so a second OTP provider is never created |
| `browser_only` is **not an accepted app type** any more | `POST /access/apps` → `12130 app type is missing or invalid` | Keep `self_hosted` |
| Whether Access challenges or denies **depends on the request headers** | bare `Accept: text/html` → 403; the same URL with `User-Agent` + `Sec-Fetch-Mode: navigate` → 200 (the Access sign-in page) | The public check asserts "never an unauthenticated 200" from a bare client, so it passes today; a browser-based probe would need to distinguish the Access page from LiteLLM's UI |
| Coder caps names at **32 characters** and reports it only after packaging | `template name "ubuntu-vscode-opencode-hermes-desktop" is invalid: must be <= 32 characters` | Renamed to `ubuntu-vscode-hermes-desktop`; checked before push, and the seed list is pinned to the limit by test |
| A **failed push of a non-default template is a note**, and the required-template check only runs in `proof` | modify finished green with the template absent | `proof` remains the gate for required templates; the name guard removes the failure mode we hit |
| `cloudflare_access` was only reachable through the advisor packs |
| The webapp block was written against an interface that **does not exist**: `hermes webapp` + `webapp.*` config keys | bootstrap log on the box: `'webapp' is not a \`hermes\` command`, and four `⚠ 'webapp.host' is not a recognized config key` warnings (whose "Did you mean: web.enabled" hints are the web-**search** section, not the server). `--help` shows the web server is `hermes dashboard` (default 9119) and `desktop` builds the native app | Rewritten against the shipped surface: `hermes dashboard --host 0.0.0.0 --port 9120 [--skip-build] --no-open`, readiness line `HERMES_DASHBOARD_READY port=9120`, pre-build `cd web && npm run build` → `hermes_cli/web_dist` (seconds, not minutes), sign-in by `POST /auth/password-login {provider,username,password}` → `{"ok":true}`, health by `/api/health` (`/health` answers 302 → `/login`, so the tile check and watchdog would have called a healthy server dead), restart by `hermes dashboard --stop` (the CLI's own process-table scan; `pkill -f "hermes dashboard"` cannot match the python-launcher cmdline). No host pinning exists, so the `public_url` config, the TF hostname local, and the wildcard-suffix token are gone | the box had **zero** Access applications | The phase runs on OTP intent, the env key is accepted on any stack, and the uninstall ledger counts the admin hostname's app and policy |
| The sign-in form rejected the operator's own username | First real click-through of the tile: `hermes-desktop--hermes-desktop-proof--adminuser.example-rootsite.com/login` answered `Invalid username or password` for `admin@example.com` and for `adminuser`. Verified with the CLI's own endpoint: username `hermes` returns `{"ok":true}`, both of those return 401. The template hardcodes `HERMES_WEBAPP_BASIC_AUTH_USERNAME="hermes"`, and the login page is rendered **before** anything in the workspace is readable, so a name that cannot be guessed has no place to be looked up | The sign-in name is now the workspace owner's Coder username (`${local.username}`), which is what a person types without reading anything, and the password stays the stack admin password they already use for LiteLLM and Dokploy. It is written with `upsert_env` (every boot) rather than `upsert_env_if_unset`, because it is derived state: a volume built when the name was `hermes` follows the owner instead of keeping a name nobody can rediscover. The bootstrap also prints the name in the startup output Coder shows while the workspace boots |
| A code-only change to shared-core rendering cannot be rolled out to an existing box | `install` refuses with "Existing install state does not match this install request" once the desired-state fingerprint moves (a new consumer-key alias, a changed default model), and `modify` only re-applies phases routed by a **changed env key**. With no env change, the box keeps running the old sidecar script while the repo says otherwise. Today the only lever is changing some key routed to that phase (the pattern knob worked). Either a `--reconcile` flag that re-applies regardless of env drift, or folding rendered artifacts into the drift check, closes this |
| Every request through the tile origin answered **400 "Invalid Host header"** | The first commit wrote `webapp.public_url = https://coder.example-rootsite.com` (the Coder admin origin), but a subdomain app is reached at `<slug>--<workspace>--<owner>.<wildcard>` and the gateway's `host_header_middleware` accepts only the bound host or the single hostname `public_url` names (`hermes_cli/web_server_boundary.py::_is_accepted_host`). The manual probe passed because the tile origin had been hand-set first — the same trap as the dashboard regression: green from inside, dead from outside | The desktop template now receives `__DOKPLOY_WIZARD_CODER_WILDCARD_SUFFIX__` and composes the tile's own origin in locals (`https://<slug>--<ws>--<owner>.<suffix>`) as `HERMES_WEBAPP_ORIGIN`. An unusable suffix (not `*.host`, or not a plain hostname) renders an empty origin and the bootstrap **clears** `public_url` instead of guessing — a wrong public_url 400s everything, while an empty one only leaves the webapp's own gate off (loopback-trusted) behind Coder `share = "owner"` + Cloudflare Access, which is the operator-accepted perimeter for this single-user box |
| The tile's **second sign-in** (the webapp's own) is the wrong product for a one-click Coder tile — supersedes the public_url row above | Read off the build: `gateway/run_api.py` engages the auth gate whenever `webapp.public_url` names a host (`should_require_auth(host) or public_hosts_engage_gate(...)`) and the `_desktop_loopback_auth_exempt` escape hatch lives only in the `public_url is None` branch the Webapp listener never takes; `hermes webapp --help` names its real UX — *"opens a one-use sign-in link"* — which a tile click cannot chase; `hermes_cli/config_defaults.py` shows `webapp` has exactly `enabled/host/port/public_url` keys (no auth key); and the WS boundary revalidates Host and Origin against the bind, so clearing `public_url` alone would refuse every proxied path. Live prototype matched the code: `public_url=""` -> tile refused; the one-use link was the only credential accepted. Operator decision (single-user box): no second sign-in | Gateway binds the **internal** port (9121) with `webapp.public_url` cleared on every boot, and `hermes-webapp-front.mjs` — a stateless loopback forwarder written by the bootstrap — owns the tile port (9120), rewrites `Host` to the bound address, and drops `Origin`/`X-Forwarded-*`, so the webapp sees a local browser on every path (HTTP middleware + WS boundary). Proven on the box before committing: external `GET /` = 200 with no redirect, Desktop bundle served, WS upgrade reached the ticket check (403 on a forged ticket, not a boundary refusal), dashboard asset 404. The watchdog distinguishes a dead front (front restart) from a dead listener (gateway restart + front), and the tile healthcheck rides the full chain |
| Passing the HTTP boundary was **not** opening the app: the tile served a healthy page that rendered a dead "Sign in to Hermes — Run hermes webapp on this computer" card | The Desktop bundle keeps its credential in sessionStorage (`apps/desktop/src/lib/browser-launch-session.ts`, key `hermes.webapp.session.v1:<origin,basePath>`) and raises `WEBAPP_LAUNCH_REQUIRED` without it; the webapp surface sets `html_token=False` and the gateway sets `withhold_session_token=True`, so the page never carries it — only the 60 s one-use launch link (`/webapp/launch`) places it, which a phone browser behind Coder cannot mint. The curl-level proof (200, correct bundle, WS passes) was blind to this: the refusal is client-side. Operator's Hermes-WebUI diagnosed and fixed it live (dist injection + a 15-line `mount_spa` patch + front-proxy injection + `~/.hermes/webapp/trust-proxy` marker) | The template's front is the credential-injecting version from the start: it reads `$HERMES_HOME/webapp/session-token` (43-char validation), injects the sessionStorage grant into `text/html` responses lacking one, recomputes `Content-Length`, and drops `ETag`; the bootstrap writes the `trust-proxy` marker every boot so the patched listener (if the operator re-applies the patch after an update) and the unpatched one both serve a signed-in page. The repo patch itself is deliberately NOT applied by the template: a tracked-file edit in a fast-moving file would make every `hermes update` a conflict, and the front injection covers the tile either way. Post-verification must now assert the served HTML contains the grant, not just a 200 |
| The Desktop tile served the **old dashboard UI**, not the browser-hosted Desktop | The template bound port 9120 with `hermes dashboard`, whose bundle is `hermes_cli/web_dist` — the admin panel, whose chat tab is a TUI over a PTY. The browser-hosted Desktop is a different artifact: the Desktop renderer at `apps/desktop/dist-webapp`, built by `hermes webapp --build-only` and mounted by the **gateway** (upstream PR #93508, merged 2026-10-25, i.e. after v0.22.5, the newest release — which is why `hermes webapp` was missing on the box and `webapp.*` read as an unknown key). Measured after switching the box onto the channel that carries it: `/` serves `dist-webapp/index.html` (asset graph contains `browser-transport-*.js`, absent from the dashboard shell), `/api/health` answers `{"ok":true,"displayVersion":"0.22.5+3088"}` unsigned, and `hermes webapp --status` exits 0 while serving / 1 while not, which gives the watchdog a real probe instead of process-table guessing. A loopback bind with no `webapp.public_url` serves the app with **no sign-in at all**, and Coder's proxy reaches it from loopback — so `public_url` is what engages the gate (`/` then answers 302 to `/login`) | Desktop template installs `--channel "$HERMES_DESKTOP_CHANNEL"` (default `main`, the only channel shipping the artifact), treats a build without `hermes webapp` as an unhealthy tile rather than falling back to the dashboard, writes `webapp.enabled/host/port/public_url` through `hermes config set`, builds with `hermes webapp --build-only`, and restarts the gateway only when `--status` says it is not serving. `hermes dashboard` appears on no executable line of the template; `default-ubuntu-code-server-hermes` keeps its dashboard untouched |

### A7. `.install-min-testing.env` follow-ups (file is gitignored via `.install*.env`)

| Item | Fix |
|---|---|
| `AI_DEFAULT_MODEL=sync/opencode-go/deepseek-v4.1-flash` | use bare `deepseek-v4.1-flash` (see A4), or drop it and let `AI_DEFAULT_MODEL_PATTERN` drive selection |
| `CLOUDFLARE_ACCESS_OTP_EMAILS` and `LITELLM_ADMIN_SUBDOMAIN` | added so the box exercises the Access application and the admin hostname; `CODER_WILDCARD_SUBDOMAIN` stays unset, which is fine because the coder pack defaults the wildcard to `*.root` |
| `LITELLM_LOCAL_API_KEY=not-needed` uncommented while `LITELLM_LOCAL_MODEL`/`LITELLM_LOCAL_BASE_URL` are commented | make the local block consistently on or off |
| sglang-over-Tailscale facts | `LITELLM_LOCAL_BASE_URL` (tailscale host/IP + sglang port + `/v1`) and `LITELLM_LOCAL_MODEL` (served model id) — example form is `http://<host>.ts.net:<port>/v1` |
| Cloudflare token | verify read-only: token verify, DNS read/write on zone `11111111…`, account Cloudflare One/tunnel, Access apps, and `Zone → SSL and Certificates → Edit` only if a nested wildcard is ever used |
| `PACKS=seaweedfs,coder` | fine for PR A/C; PR B's upgrade report will cover the coder/seaweedfs/LiteLLM/tunnel slice |

---

## PR B — `feature/stack-upgrade`

Goal: a real stack-wide update command. Today `install` and `modify` are the only
paths, `modify` no-ops when compose is unchanged, and several images are pinned
in code with no lever to bump them — which is how a LiteLLM CVE and a stale
cloudflared survive unnoticed.

### B1. Component inventory (verified from code)

| Component | Pin today | Refresh mechanism | Gap | Lever |
|---|---|---|---|---|
| Dokploy core | `install.sh` tip, at install only | Dokploy's own UI | wizard never re-applies it | `upgrade` step + version report |
| Coder server | `ghcr.io/coder/coder:latest` | container recreate | a no-op `modify` does **not** re-pull `:latest` | force redeploy + report running vs registry digest |
| cloudflared | `cloudflare/cloudflared:latest` | same | invisible drift | same |
| LiteLLM | **pinned** `v1.83.14-stable` (`LITELLM_IMAGE_TAG`) | env override only | manual bump, hand-edited | `upgrade --litellm <tag>` → compose hash changes → real redeploy |
| Nextcloud / OnlyOffice | `nextcloud:apache`, `onlyoffice:latest` | recreate | majors need `occ upgrade` + maintenance mode | version-gated step, refuse majors without `--allow-major` |
| Moodle | `moodle-php-apache:8.4-bullseye` | code edit only | PHP CVEs cannot be fixed without a code change | env override + bump in `moodle.py` |
| Postgres / Redis | `pgvector/pgvector:pg16`, `redis:7-alpine` | recreate | major bumps need dump/restore | patch upgrades allowed, majors refuse with a plan |
| SeaweedFS / Headscale / Synapse / Docuseal / postfix | `:latest` | recreate | no drift visibility | drift report |
| SurfSense | `rocicorp/zero:1.4.0`, `searxng:2026.3.13-…` | code edit | deliberately stale | bump in code (moot after PR C) |
| Workspace image + CLIs | `codercom/enterprise-base:ubuntu`, Node 24, pnpm 10.27.0, opencode install.sh, `pi` latest, hermes install.sh @ main | workspace boot | no version report; `hermes update` never runs | boot-step version report + opt-in `hermes update` |

### B2. Options and shape

| Option | What | Effort | Buys |
|---|---|---|---|
| O1 | `inspect-state --check-updates`: running vs newest registry digest per service | small | see the CVE surface in one table, no behavior change |
| O2 | `upgrade [--services …]`: force re-pull + gated redeploy of floating services, then reseed Coder templates | medium | "update all the things" in one command |
| O3 | `versions.json` release manifest holding every non-floating tag + `upgrade` rewrites it | medium | reproducible installs, reviewable bumps, no code edit for a CVE |
| O4 | O1+O2+O3 + `hermes update` in workspaces + workspace template/agent updates | large | whole stack including Coder workspaces |
| O5 | Nightly throwaway-VPS `proof` run + image-resolvability check | separate | catches upstream breakage before users do |

| Phase | Action | Gate | Rollback |
|---|---|---|---|
| 1 | Plan: read ledger, diff running vs newest tags | always | — |
| 2 | Snapshot: `hermes backup`, `pg_dump` shared core, state checkpoint | required before apply | restore from artifacts |
| 3 | Apply per service in dependency order (core → LiteLLM → packs → Coder) | health check per service, no advance on failure | previous tag from ledger, redeploy |
| 4 | Coder templates reseed (content-hash already does the right thing) | existing | revert template dir |
| 5 | Workspaces: report which are behind, update agents, restart vs recreate only on request | explicit flag | recreate is the only destructive one |
| 6 | Report + ledger write | — | — |

Watch-outs: `:latest` is a lie until something re-pulls it; Postgres major bumps
need dump/restore; Nextcloud majors need `occ upgrade`; LiteLLM tag bumps must be
operator-approved, never silent; PR B and PR C both touch `versions.json` and the
README table, so whichever lands second rebases.

---

## PR C — `feature/remove-agent-packs`

Goal: remove OpenClaw (user-visible "Nexa Claw"), Nexa (its Nextcloud/Talk/
OnlyOffice runtime), Telly (its Telegram persona), My Farm Advisor ("Nexa Farm",
an OpenClaw-derived variant), and SurfSense (research/chat pack). Everything
else stays. Hermes inside Coder is the agent path forward.

### C1. Footprint

| Removed (measured, excludes `.sisyphus/`, `build/`, caches) | Files | LOC |
|---|---|---|
| Nexa src + its Dockerfiles + fixtures + 12 Nexa test files | 23 | 8,193 |
| OpenClaw unit + integration tests | 2 | 5,029 |
| `dokploy/openclaw.py` (3,464) + surviving `packs/openclaw/{__init__,models,reconciler}.py` | 4 | 4,364 |
| SurfSense src (`dokploy/surfsense.py`, `surfsense_backend.py`, `packs/surfsense/`) + 2 test files | 7 | 4,057 |
| openclaw compose template, farm compose template, 2 farm `.env.example`, incident doc | 6 | ~240 |
| **Total deleted** | **~42-46** | **~21,800** |
| Edited in place | ~85 files | floor ~3,900 mention-lines |
| Test functions touched | ~350-400 across ~50 files (`tests/test_cli.py` alone ≈100) | |

Verified zero references (clean survivors): `matrix`, `headscale`, `docuseal`,
`moodle`, `onlyoffice`, `tailscale`, `seaweedfs`, `bootstrap`,
`remote_transport`, `bin/*`.

Structural notes: Nexa and Telly live **inside** `packs/openclaw/` /
`dokploy/openclaw.py`; My Farm Advisor is a **variant switch inside the OpenClaw
backend**, so deleting all five at once is cleaner than any subset — that is why
it is one PR.

### C2. Decisions

| Topic | Decision |
|---|---|
| Retirement depth | **code-only removal.** No tombstones, no decommission command. Existing boxes are reinstalled from scratch, never upgraded into the new tree |
| Nextcloud side | strip all advisor plumbing: `_ensure_nexa_service_account`, the `/Nexa Claw` `files_external` mount, `_ensure_openclaw_rescan_schedule` + `NEXTCLOUD_OPENCLAW_RESCAN_*` keys, `DOKPLOY_WIZARD_OPENCLAW_*` env writes, `NextcloudOpenClawWorkspaceContract`. Talk/OnlyOffice stay for humans |
| Shared Postgres | keep `pgvector/pgvector:pg16` (avoid re-creating live data); drop the per-DB `SUPERUSER` grant, `CREATE EXTENSION vector`, and `wal_level=logical` + replication-slot args that existed only for SurfSense Zero (`_allocation_requires_surfsense_postgres_capabilities`, `shared_core.py:243,2095`) |
| Ordering | PR A first, then PR C rebases onto the smaller tree |

### C3. There is no pack-retirement mechanism today

`_retired_template_names()` (`coder.py:1124-1129`, best-effort delete at
`:387-398`) proves the codebase can retire a *Coder template*. Nothing analogous
exists for packs, so a code-only removal bites five ways:

| # | Failure | Evidence | Consequence here |
|---|---|---|---|
| 1 | old `.install.env` still names `PACKS=…,openclaw,…` | `packs/resolver.py:99-103` `Unknown pack selection(s) in PACKS` | this repo's `.install.env:26` and `.dokploy-wizard-state/install.env:19` both list `openclaw` → clean them or use `.install-min*.env` |
| 2 | checkpoint refuses retired completed steps | `state/store.py:714-737` allowlist | `StateValidationError: existing applied state contains unsupported completed steps` |
| 3 | uninstall hard-errors on unknown ledger resource types | `uninstall/planner.py:246-252` | uninstall refuses on an old box |
| 4 | `CLOUDFLARE_ACCESS_OTP_EMAILS` is **gated on** those packs | `state/env.py:502-510`; `_access_target_hostnames` = `{openclaw, my-farm-advisor, litellm-admin}` (`networking/planner.py:569-589`) | **must survive PR C**: re-key onto `litellm-admin`/`coder` so Access OTP stays configurable for future apps even though no shipped pack needs it now |
| 5 | `DesiredState.from_dict` *requires* the dropped fields | `state/models.py:524-530` | old `desired-state.json` unreadable — accepted under code-only removal |

### C4. Stranded on any box that ran those packs (accepted, no cleanup)

Dokploy compose apps and services (`mem0`, `qdrant`, `nexa-runtime`,
`openclaw-public`); 4 Docker volumes that `_PACK_DATA_RESOURCE_TYPES["openclaw"]
= ()` never tracked (`-openclaw-data`, `-public-data`, `-mem0-history`,
`-qdrant-data`) so they survive even `--destroy-data`; per-pack Postgres DB,
role, `SUPERUSER`, `vector` extension; LiteLLM virtual keys
`sk-litellm-openclaw`/`-my-farm-advisor`/`-surfsense` that keep rendering
`LITELLM_VIRTUAL_KEY_OPENCLAW` into shared-core env and keep being pushed to the
sync sidecar; Cloudflare DNS/tunnel routes/Access apps for `openclaw.`, `farm.`,
`surfsense`, `surfsense-api`, `surfsense-zero`; Nextcloud `nexa` service account,
`/Nexa Claw` mount, `{stack}-openclaw-rescan` Dokploy schedule (`delete_schedule`
exists and is never called for it); stale Dokploy env specs whose
`target_services` name deleted services (nothing validates that direction,
`env_spec.py:115-148`); built images `local/dokploy-wizard-nexa-*`.

### C5. Code that dies of neglect (must be re-keyed, not deleted)

| Orphan | Action |
|---|---|
| `preflight.derive_required_profile()` `FULL_PACK_SET_PROFILE` trigger is `openclaw or my-farm-advisor` | re-key on the heavy packs that remain (`nextcloud`, `matrix`, `coder`) or host sizing checks silently relax |
| `packs/prompts.py` guided prompts and `OPENCLAW_*` keys the farm consumed (`OPENCLAW_TELEGRAM_GROUP_POLICY`, `OPENCLAW_SYNC_SKILLS_*`, `ADVISOR_GATEWAY_PASSWORD`) | delete with the farm after confirming no survivor reads them |
| `qa_harness.py:238` hard-indexes `virtual_keys['openclaw']`; `remote.py:609` hard-indexes pack names | rewrite; add a grep-gate test asserting zero surviving references in `src/` |
| SurfSense-only lifecycle phases (2 in `PHASE_ORDER`, 1 drift branch), `SurfSenseGeneratedSecrets` + state file, 9 `cli.py` plumbing sites | unwrap |
| `fixtures/invalid-pack-combo.env` + `tests/helpers/root_install_env.py` bake the removed names | rewrite; `invalid-pack-combo.env` needs a new offending pack to keep exercising that error path |
| 5 farm-only tests inside OpenClaw test files | relocate, do not delete |
| LiteLLM consumer lists in `config_renderer.py` / `model_sync.py` | **no edit needed** — data-driven. Only producers (`state/models.py`, `shared_core.py`) change |

### C6. README / docs surgery (no rewrite)

Delete outright: `### OpenClaw`, `### Nexa`, `### Telly`,
`### Nexa credential mediation`, `### My Farm Advisor`, `### SurfSense`,
`## My Farm Advisor operator reference` (README:794-932) — ≈250 of 948 lines.
Edit in place ≈20 sections: product definition (README:21-24), pack list,
hostnames (drop the `openclaw.<root>` example), prerequisites/resource sizing,
verification, uninstall, secrets/credential tables, troubleshooting, LiteLLM
consumer/consumer-key docs, the `docs/incidents/openclaw-trusted-proxy-scopes.md`
link, and `AGENTS.md`'s pack list + focused-modules block. Leave
`.sisyphus/evidence/**` alone — historical evidence, not docs.

### C7. PR C watch-outs

| Watch-out | Mitigation |
|---|---|
| PR A and C both edit README, `.env.example`, and model defaults | land A first; C rebases |
| Model-default sites shrink under C (`openclaw.py:121-122`, `surfsense.py`) | whoever lands second re-greps every site so `deepseek-v4-flash` cannot be resurrected from a fixture |
| Tests are the real cost, not the deletions | ~70 % of the effort is ~350-400 test-function edits |
| `state/models.py::DesiredState.from_dict` requires dropped fields | `inspect-state` on an old box is dead, not degraded — accepted |
| Optional 5-line insurance kept unless told otherwise | leave `openclaw_service` / `my_farm_advisor_service` / `surfsense_service` delete rules in `uninstall/planner.py::_RULES` so a box that *does* have them can still be uninstalled instead of hard-erroring |

---

## Decision log (from AskUserQuestion rounds)

| Question | Answer |
|---|---|
| 503 repro detail | restart fixes it; fails "live while other apps work"; also "stopped after lots of chats" |
| Template naming | new dir named for the desktop web app, not "-pi" |
| Where to build | `ubuntu-vscode-hermes-desktop` cloned from `-pi`, "all the fixes and extras" |
| Official surface | gateway-served Desktop **webapp**, must "just work in a tab with full functionality" |
| Tile wiring | Coder subdomain app |
| Webapp sign-in | bundled username/password provider seeded from the stack admin password |
| Default tile | official primary, community fallback, **no** `hermes dashboard` surface |
| Model default | provider as-is (opencode-go today; openrouter/NVIDIA equally fine), model chosen by **string match + highest version / most recently updated**, validated by a real completion probe for the default only; other models validated by the live list/metadata endpoints, never one probe per model |
| Model machinery | reuse the existing `litellm-model-sync` sidecar — no second sidecar |
| Availability filtering | probe the OpenRouter free slice, fix the NVIDIA probe, keep OpenCode Go's honest list |
| Upgrade scope | full stack-upgrade command as its own PR (PR B) |
| Removal scope | one PR, all five packs, code-only removal, strip Nextcloud advisor plumbing, keep pgvector image but drop SurfSense-only privileges |
| Environment | no local Docker at all; remote-only testing to `203.0.113.10` with `.install-min-testing.env`; only Cloudflare zone `11111111111111111111111111111111` |

## Verification backlog

| Item | Status |
|---|---|
| Live LiteLLM `/v1/models` alias form for the default (`opencode-go/…` vs `sync/opencode-go/…`) | resolved by A4: bare id in env; confirm on the deployed box |
| `.install-min-testing.env` Cloudflare token scope check | not yet run |
| sglang-over-Tailscale base URL + served model for `LITELLM_LOCAL_*` | user must supply (or approve `tailscale ip`/port discovery) |
| Newest `litellm/litellm:*-stable` tag + CVE list for the pinned `v1.83.14-stable` | PR B, operator-approved bump |
| Whether `*.example-rootsite.com` resolves and is tunneled | resolved: the record and the tunnel rule exist, and `wildcard_access_url` reports `*.example-rootsite.com` |
| Admin hostname reachable end to end | resolved: internal `/ui` answers 200 through the redirect to `/ui/`, `/` redirects to `/ui`, `/docs` answers 200, and the public hostname puts the Access sign-in page in front |
