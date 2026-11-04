# Contributors

Everything an operator guide (`AGENTS.md`) isn't: how the moving parts actually work, how to
change them safely, and how we prove changes on a real box. Read `AGENTS.md` first for
commands and secret rules; this file explains the machinery.

## Repo map (the parts that matter)

| Path | Role |
|---|---|
| `src/dokploy_wizard/` | The wizard: phases, packs, ownership ledger, modify/uninstall logic. |
| `templates/coder/default-ubuntu-vscode-hermes-desktop/` | The Coder workspace template with the Hermes Desktop tile (the front proxy + bootstrap heredocs live in `main.tf`). |
| `tools/verify_hermes_webapp_block.py` | Static + behavioural gate for the template heredocs (`bash -n`, `node --check`, Terraform `$${` escape guard, injection/CSP unit run). Run it whenever you touch the template. |
| `docs/plans/` | Plans of record + findings tables. Reasoning belongs here, not only in PR prose. |
| `docs/incidents/` | Post-mortems of things that broke on the test box. |
| `tests/unit`, `tests/integration`, `tests/e2e` | The local gate. Everything below must stay green before any commit. |

## The local loop (no Docker on your machine)

This repo is developed **without local Docker**. Installs, modifies, and proofs run against a
disposable Contabo VPS through the remote helper:

```bash
./bin/dokploy-wizard-remote proof --host <ip> --password '<root>' --env-file .install-min-testing.env
```

- `.install.env` (full pack list) and `.install-min-testing.env` (`PACKS=seaweedfs,coder`)
  stay at repo root, mode `0600`, and are gitignored. They are the only place raw
  credentials live; runtime-generated secrets land in the state directory on the host.
- Remote runs are long (10–60 min, image pulls dominate). **Never kill them on a short
  timeout**: pass ≥ 3,600,000 ms tool timeouts or run in the background. A `still running ...
  waiting for output` line means healthy, not stuck.
- If a run died at the Dokploy bootstrap health check, remove the half-built bootstrap on the
  host (`docker service rm dokploy dokploy-postgres`) before retrying, or the preflight port
  check rejects 3000.
- Gate before **every** commit:

  ```bash
  uv run --all-extras pytest -q && ruff check . && mypy .
  ```

## How the Hermes Desktop tile works

Three cooperating pieces, all inside the template (details + diagrams: PR #13 body and
`docs/plans/hermes-desktop-webapp-and-follow-ons.md`):

1. **Bootstrap** (bash heredoc in `main.tf`) — writes the `~/.hermes/webapp/trust-proxy`
   marker each boot, resolves `sync/default-chat` from the gateway's `/v1/models`, and starts
   `hermes web` on `127.0.0.1:9121` with `web_server.public_url` **cleared** (the only shape
   where redirects stay relative and the front can own the URL + fragment).
2. **Front proxy** (Node heredoc) — owns the Coder tile port `9120`; rewrites `Host` to the
   app's bind host (host-allowlisting), drops `Origin`/`X-Forwarded-*` (same-origin checks),
   rewrites `/api/*` reply URLs onto its own origin, injects the sessionStorage grant
   (`hermes.webapp.session.v1` → `[location.origin, basePath]`), and **appends its own
   `'sha256-…'` to the `script-src` of the CSP header** — a hash added only to the `<meta>`
   tag is ignored, because the strictest CSP source wins. This is the classic
   "curl proves pass, browser still shows the sign-in card" trap.
3. **The in-workspace watchdog** (systemd user unit) — revives the Hermes WebUI, gateway
   shim, or app proxy from one place; the liveness probe counts only failures a wait cannot
   fix, so slowness alone never restarts anything.

The periphery that makes this safe: tile apps have `share = "owner"`, nothing is
port-published, everything rides the Cloudflare Tunnel, and Coder's own login is the app
auth at the edge.

## Talking to the running box

- SSH is root-password over `paramiko`; env values come from the local env file, never echoed.
- Workspace code lives in a container named `coder-<user>-<workspace>` on the host; run things
  as `docker exec <container> sudo -u coder bash -lc '...'`.
- Coder API from the host itself (bypasses TLS/SNI): `curl -sk -X POST
  'https://127.0.0.1/api/v2/users/login?withToken=true' -H 'Host: coder.<root>' -d @login.json`.
- Coder app hosts follow `<app-slug>--<workspace>--<user>.<root>` (slugs: `hermes-desktop`,
  `hermes-webui`, `code-server`, `opencode`).
- `opencode serve` password quirk: the value the running server accepts is the **boot-time
  env** `OPENCODE_SERVER_PASSWORD`; `opencode service get password` can be stale/different,
  and `/api/info` 401s transiently across serve restarts. Read the live process env
  (`/proc/<pid>/environ`) when in doubt.

## Playwright notes (browser proofs from the workstation)

- Bypass the Cloudflare edge locally with
  `--host-resolver-rules=MAP <root>, MAP *.<root> <vps-ip>` + `ignoreHTTPSErrors` — this skips
  Access-gated interstitials on purpose; take one real-public shot separately when the Access
  path itself is the subject.
- Coder's login form is `#email` + `button:has-text('Sign In')` (not `type=submit`); the
  session is the httpOnly `coder_session_token`. Persist authenticated state to a
  `storage.json` and reuse it across scripts.
- Hermes Desktop sign-in (when the legacy dashboard path is used) takes the workspace owner's
  Coder username + the stack admin password; it is printed into the workspace boot output.

## Screenshot / evidence conventions

- **No binaries in the git tree, ever.** Screenshots are uploaded as GitHub **release assets**
  (e.g. the `visual-proof-assets` release) and referenced from PR bodies/comments by their
  download URL.
- Every screenshot that could contain identity must pass the pixelate pass: capture a per-shot
  JSON manifest of sensitive DOM rects (text-node walk matching a configured identity regex —
  root-domain label, operator username, email addresses, company name, `sk-…` key shapes)
  while photographing, then blur those rects with PIL (`scale` recorded per
  device-pixel-ratio). Verify the **final** image by
  looking at it; input-field values (filled emails) are invisible to the DOM walk and need an
  explicit rect.
- Blur checklist before anything lands in a PR: passwords, API keys, key names, emails, the
  operator username, the real root domain, Cloudflare team-domain lines.

## Findings discipline

- Measured facts go into `docs/plans/*` findings tables ("what the box measured, not what the
  plan assumed"), including negative results — rejected approaches in the PR body exist for
  the same reason: they must be rediscoverable.
- A shared-core code change without a rollout against a live stack is **not proven**; say so
  explicitly rather than implying it works.
- Cloudflare safety: only the one zone/account named in your env file may be touched; the
  Access OTP path (`/ui/` on the admin hostname is the guarded one) must keep working through
  every pack change.

## Where to land things

- Template-only work → this tree's template dir + the static verifier + the live CDP proof.
- Shared-core (litellm admin, naming, lifecycle) → include a rollout against the min stack,
  or document the gap in the plan file and the PR body.
- Stack upgrades and pack removals travel as separate PRs on top of the merged template work.
