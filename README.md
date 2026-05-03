# Dokploy Wizard

Dokploy Wizard is a Python-first installer for standing up a real self-hosted stack on a fresh Ubuntu VPS with Dokploy, Cloudflare Tunnel, optional Cloudflare Access, optional Tailscale host access, and a set of opinionated application packs.

Today this repo is not a scaffold or mock planner. It performs real deployment, real rerun/modify/uninstall flows, and has been validated on fresh VPS rebuilds.

## What it installs

- **Dokploy** as the deployment control plane
- **Cloudflare Tunnel** for public ingress
- **Cloudflare Access** for browser-safe advisor surfaces
- **Tailscale** for private/admin host access
- **Shared Core** services used by packs
  - PostgreSQL
  - Redis
- **Nextcloud + OnlyOffice + Talk**
- **Moodle**
- **DocuSeal**
- **OpenClaw** (user-visible name: **Nexa Claw**)
- **Nexa**, embedded inside OpenClaw as the Nextcloud/Talk/OnlyOffice-facing agent runtime
- **Telly**, embedded inside OpenClaw as the Telegram-facing agent persona
- **My Farm Advisor** (user-visible name: **Nexa Farm**) — separate advisor runtime with Field Operations and Data Pipeline workspaces
- **SeaweedFS** for S3-compatible object storage
- **Coder** with a seeded Ubuntu + VS Code workspace template

Optional packs also include Headscale, Matrix, and Coder. My Farm Advisor is optional but can run side-by-side with OpenClaw.

## Current reality

- Fresh-VPS install works
- Same-host rerun / noop proof works
- fresh-VPS install, rerun, and inspect-state flows are part of the validation path
- OpenClaw, Nexa, Nextcloud Talk, OnlyOffice, SeaweedFS, and Coder are all part of the wizard-managed path

## High-level architecture

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "primaryColor": "#eef6ff",
    "primaryTextColor": "#10243e",
    "primaryBorderColor": "#8eb8e8",
    "lineColor": "#5f7ea3",
    "secondaryColor": "#f7f9fc",
    "tertiaryColor": "#fffaf0",
    "clusterBkg": "#fbfdff",
    "clusterBorder": "#c9d9ee",
    "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif"
  }
}}%%
flowchart LR
    Internet([🌍 Internet]) --> CF[☁️ Cloudflare Tunnel]
    CF --> Traefik[🚦 Dokploy / Traefik ingress]

    Tailnet([🔐 Tailscale Tailnet]) --> Host[🖥️ Host access]
    Host --> Dokploy[📦 Dokploy]

    subgraph Apps[Wizard-managed application surfaces]
      OpenClaw[🤖 OpenClaw]
      Nextcloud[🗂️ Nextcloud]
      OnlyOffice[📝 OnlyOffice]
      Coder[💻 Coder]
      Seaweed[🪣 SeaweedFS / S3]
    end

    subgraph Internal[Internal-only sidecars and shared services]
      Shared[(🧱 Shared Core\nPostgres + Redis)]
      Mem0[🧠 Mem0]
      Qdrant[🔎 Qdrant]
      NexaRuntime[🧵 Nexa runtime]
    end

    Traefik --> OpenClaw
    Traefik --> Nextcloud
    Traefik --> OnlyOffice
    Traefik --> Coder
    Traefik --> Seaweed
    Dokploy --> Shared
    Dokploy --> OpenClaw
    Dokploy --> Nextcloud
    Dokploy --> Coder
    Dokploy --> Seaweed

    OpenClaw --> Mem0
    OpenClaw --> Qdrant
    OpenClaw --> NexaRuntime
    NexaRuntime --> OpenClaw
    NexaRuntime --> Nextcloud
    Nextcloud --> OnlyOffice
    Apps --> Shared
```

## Install and lifecycle flow

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "primaryColor": "#f4fbf6",
    "primaryTextColor": "#173226",
    "primaryBorderColor": "#8fc8a8",
    "lineColor": "#5c8f74",
    "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif"
  }
}}%%
stateDiagram-v2
    [*] --> Input : guided install\nor env file
    Input --> DesiredState : resolve hostnames\nsecrets\npacks
    DesiredState --> Preflight
    Preflight --> DokployBootstrap
    DokployBootstrap --> Tailscale
    Tailscale --> CloudflareNetworking
    CloudflareNetworking --> CloudflareAccess
    CloudflareAccess --> SharedCore
    SharedCore --> Headscale
    Headscale --> Matrix
    Matrix --> NextcloudOnlyOffice
    NextcloudOnlyOffice --> SeaweedFS
    SeaweedFS --> OpenClaw
    OpenClaw --> MyFarmAdvisor
    MyFarmAdvisor --> Installed

    Installed --> Modify
    Installed --> Resume
    Installed --> Uninstall
    Modify --> DesiredState
    Resume --> Preflight
    Uninstall --> [*]
```

## What each major surface does

### Dokploy

Dokploy is the control plane the wizard targets for all compose app deployment. The wizard bootstraps Dokploy, mints or reuses an API key, and then uses that API key for all managed pack operations.

### Shared Core

Shared Core is the common substrate for packs that need databases or cache services. Today that means:

- PostgreSQL for Coder, Nextcloud, and OpenClaw
- Redis for Nextcloud

These are wizard-owned resources, tracked in the ownership ledger, and reused across modify / rerun operations.

### Nextcloud + OnlyOffice + Talk

The `nextcloud` pack always includes OnlyOffice as its paired document editor runtime and Talk for real-time chat/voice/video.

What the wizard wires today:

- Nextcloud service + persistent volume
- OnlyOffice service + persistent volume
- shared-core Postgres + Redis bindings
- trusted domain configuration
- OnlyOffice JWT configuration wiring
- Nextcloud Talk app verification
- OpenClaw workspace mount into Nextcloud when Nexa is enabled

### OpenClaw

OpenClaw is deployed as an advisor runtime with:

- browser-facing access through Cloudflare Tunnel
- Cloudflare Access OTP protection on its public hostname
- trusted-proxy browser auth for the Control UI
- generated gateway config and agent bindings
- `gateway.http.endpoints.responses.enabled = true` so internal adapters can hand requests into OpenClaw’s own runtime

### Nexa

Nexa is **not** a standalone pack. It is embedded inside the OpenClaw deployment when `OPENCLAW_NEXA_*` env values are present.

Nexa’s current role is to bridge OpenClaw into the Nextcloud ecosystem:

- Nextcloud Talk message handling
- Nextcloud file creation and sharing via WebDAV/OCS
- OnlyOffice callback ingestion and reconcile contract wiring
- Mem0-backed memory lookup and memory write policy
- OpenClaw pass-through for grounded tool use and command execution

The important design point is that Nexa is not just a prompt file. The wizard deploys a dedicated `nexa-runtime` sidecar and associated contract files under the OpenClaw volume, but some Talk and OnlyOffice behaviors are still intentionally conservative and continue to depend on upstream OpenClaw/runtime behavior.

### Telly

Telly is the Telegram-facing OpenClaw agent persona. Like Nexa, it is seeded by the OpenClaw deployment code rather than installed as a separate pack.

What the wizard currently does for Telly:

- seeds a dedicated Telegram-facing agent persona
- binds the Telegram channel to `telly`
- configures Telegram DM allowlist / ownership values from `OPENCLAW_TELEGRAM_*`
- seeds a `workspace-telly` with operator-facing guidance files

### My Farm Advisor

My Farm Advisor is a separate advisor runtime from OpenClaw. It runs its own container image (`ghcr.io/borealbytes/my-farm-advisor:latest`) on its own hostname (default `farm.<root-domain>`) and does not share the OpenClaw Nexa sidecars.

What the wizard does for My Farm Advisor:

- deploys a standalone service with its own Dokploy compose app
- wires Cloudflare Access OTP on `farm.<root-domain>`
- seeds two workspaces under `/data`: `workspace` (field ops) and `workspace-data-pipeline`
- mounts both workspaces into Nextcloud as external storage when Nextcloud is enabled
- maps wizard env keys into the container using a dedicated farm runtime contract so farm-only flags cannot leak into OpenClaw

My Farm Advisor and OpenClaw can run side by side. There is no slot conflict.

### Coder

Coder gets a seeded Ubuntu + VS Code template and a first default workspace.

On first successful bootstrap the wizard:

- provisions the initial Coder admin
- pushes the seeded templates:

  | Template | What it provides |
  |---|---|
  | `ubuntu-vscode` | Base Ubuntu + VS Code with `curl`, `git`, `wget`, `btop`, `opencode`, `zellij` |
  | `ubuntu-vscode-opencode-web` | OpenCode Web (browser-based IDE) |
  | `ubuntu-vscode-openwork` | OpenWork (AI-assisted workspace) |
  | `ubuntu-vscode-kdense-byok` | K-Dense BYOK (Bring Your Own Key for local model inference) |
  | `ubuntu-vscode-hermes` | Hermes (on-device AI assistant) |

- creates a default workspace for the operator

That default template installs:

- `curl`
- `git`
- `wget`
- `btop`
- `opencode`
- `zellij`

The workspace home directory lives on a per-workspace Docker volume. The control plane state lives in shared-core Postgres.

### Coder App Hostnames

The wizard supports two different ideas for how browser-facing Coder apps should be routed:

- The ideal Coder-native shape is `*.coder.<root-domain>`.
- The currently selected no-fee fallback is `*.<root-domain>` limited by a strict app-host pattern, so only Coder app-style hostnames route into Coder.

Why the fallback exists:

- Cloudflare Universal SSL on a full zone covers the zone apex and first-level subdomains.
- A hostname like `foo.coder.example.com` is a deeper subdomain and is not covered by Universal SSL.
- Cloudflare's supported paid fix is Advanced Certificate Manager, which can issue edge certificates for `*.coder.<root-domain>`.

Current decision:

- Keep the fallback `*.<root-domain>` for live installs until a future architecture change is chosen.
- Preserve a strict router pattern so service hosts like `dokploy.<root-domain>`, `nextcloud.<root-domain>`, and `openclaw.<root-domain>` are not hijacked by Coder.

Future architecture options:

1. Keep the current fallback: lowest risk and already working. Coder app hosts look like `app--workspace--user.<root-domain>`.
2. Use `*.coder.<root-domain>` with Cloudflare Advanced Certificate Manager: closest to the Coder docs and cleanest hostname model, but requires ACM on the zone.
3. Keep `*.coder.<root-domain>` but move Coder off Cloudflare Tunnel: terminate public TLS directly in Dokploy/Traefik using DNS-01 wildcard certificates. This avoids the ACM fee but changes the ingress architecture for Coder.
4. Convert selected Coder apps to path-based routing: works without wildcard subdomain TLS, but app compatibility is weaker than subdomain routing and usually requires template-specific base-path work.

Operational note:

- Hermes, OpenCode Web, and OpenWork are already path-based Coder apps in this repo.
- K-Dense BYOK is the current outlier that uses `subdomain = true` and benefits the most from proper wildcard app routing.

### SeaweedFS

SeaweedFS provides the S3-compatible object-storage surface. The wizard wires:

- service + data volume
- generated access key + secret key in guided mode
- public `s3.<root-domain>` hostname

## Auth and credential model

### Auth boundaries at a glance

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "primaryColor": "#fff8ef",
    "primaryTextColor": "#3b2b10",
    "primaryBorderColor": "#e7bb74",
    "lineColor": "#a77834",
    "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif"
  }
}}%%
flowchart TD
    CFOTP[✉️ Cloudflare Access OTP] --> OpenClaw[openclaw.<root-domain>]
    CFOTP --> Farm[farm.<root-domain>]

    subgraph AppLogins[Own login surfaces]
      Dokploy[Dokploy admin login]
      Nextcloud[Nextcloud admin + users]
      Coder[Coder admin login]
      Seaweed[SeaweedFS access key / secret key]
    end

    subgraph InternalSecrets[Server-owned runtime secrets]
      Nexa[Nexa Talk/WebDAV/OnlyOffice secrets]
      DB[Database / Redis secret refs]
      Model[OpenRouter / NVIDIA / local model config]
    end
```

### Which surfaces are behind OTP

Current Cloudflare Access scope:

- `openclaw.<root-domain>`
- `farm.<root-domain>`

Not behind Cloudflare Access in the current implementation:

- `dokploy.<root-domain>`
- `nextcloud.<root-domain>`
- `office.<root-domain>`
- `s3.<root-domain>`
- `coder.<root-domain>`
- Coder workspace app hosts, currently routed via a controlled `*.<root-domain>` fallback
- `matrix.<root-domain>`
- `headscale.<root-domain>`

Why:

- Dokploy still needs a usable API/control plane path
- Nextcloud and OnlyOffice have client/protocol concerns
- SeaweedFS is an object-storage protocol surface
- Coder has its own application login
- Matrix and Headscale are protocol/control-plane surfaces

### Which things have their own login/credentials

| Surface | Credential model | Source |
|---|---|---|
| Dokploy | admin email + password, then API key | operator-supplied + wizard-generated API key |
| OpenClaw | Cloudflare Access OTP + trusted-proxy browser auth, plus gateway password/token surfaces | generated gateway password, optional token |
| Nextcloud | admin user/password and internal service accounts | currently derived from Dokploy admin credentials, plus Nexa service account from env |
| OnlyOffice | JWT integration value shared with Nextcloud | currently wired by deployment bootstrap/runtime config |
| Coder | Coder admin login | currently derived from Dokploy admin credentials |
| SeaweedFS / S3 | access key + secret key | wizard-generated in guided mode or env-file provided |
| Nexa internals | Talk/WebDAV/OnlyOffice/API secrets | server-owned env |

### Credential sources

The wizard currently uses three broad credential sources:

1. **Operator-supplied values**
   - Cloudflare token/account/zone
   - Dokploy admin login
   - Tailscale auth key
   - model/provider API keys

2. **Wizard-generated values**
   - SeaweedFS access key + secret key in guided mode
   - OpenClaw browser/control password in guided mode
   - My Farm Advisor browser/control password in guided mode
   - Dokploy API key after bootstrap

3. **Server-owned runtime secrets**
   - Nextcloud and Nexa runtime integration values
   - Nexa Talk signing/shared secrets
   - Nexa WebDAV auth
   - Nexa service-account credentials
   - Mem0/Qdrant private runtime configuration

### Nexa credential mediation

Nexa is intentionally treated as a server-owned runtime surface.

That means:

- the wizard keeps sensitive Nexa values in env/config surfaces owned by the deployment
- the Nextcloud-visible Nexa workspace is an operator/user surface, not the source of truth
- the runtime contract records **presence and source**, not raw secret values

## Current install file expectations

The current repo uses `.install.env` at repo root as the working operator env file.

Important details:

- it contains sensitive credentials and should remain `0600`
- the fresh-VPS harness copies it explicitly to the remote host
- Nexa functionality is enabled by the presence of `OPENCLAW_NEXA_*` values
- runtime-only values like internal sidecar URLs are synthesized later during deployment

## Current fresh-VPS proof status

The repo includes a real fresh-VPS proof flow with:

- first install success
- same-host rerun/noop success
- `inspect-state` execution as part of the proof loop

The local proof artifacts live under:

- `.sisyphus/evidence/fresh-vps-validation/fresh-reinstall-live-proof/`

What that means in practice:

- the wizard can package the repo and install env
- copy both to a fresh host
- run the installer non-interactively
- rerun it on the same host
- inspect the resulting state as part of the same reproducibility loop

The checked-in proof artifact is useful as a concrete example run, but it should not be treated as the only source of truth for current drift status after later inspection fixes.

## Install modes

### Guided first-run install

```bash
./bin/dokploy-wizard install
```

Use this when you do not already have an env file. The wizard prompts for domains, Dokploy credentials, Cloudflare values, optional Tailscale settings, and pack selection, then writes a reusable env file and runs the same install flow as env-file mode.

### Env-file install

```bash
./bin/dokploy-wizard install --env-file path/to/install.env --non-interactive
```

### Inspect state

```bash
./bin/dokploy-wizard inspect-state --env-file path/to/install.env --state-dir .dokploy-wizard-state
```

### Modify / rerun / uninstall

```bash
./bin/dokploy-wizard modify --env-file path/to/install.env --non-interactive
./bin/dokploy-wizard uninstall --retain-data --non-interactive --confirm-file fixtures/retain.confirm
./bin/dokploy-wizard uninstall --destroy-data --non-interactive --confirm-file fixtures/destroy.confirm
```

## Fresh-VPS harness

The repo also contains a real fresh-host validation harness:

```bash
python -m src.dokploy_wizard.fresh_vps_validation_harness \
  --install-env-file ./.install.env \
  --target-host <host> \
  --target-user root \
  --target-password <password> \
  --target-path /root/dokploy-proof \
  --label proof-run
```

What it does:

- packages the repo
- uploads repo + `.install.env`
- runs wizard install
- reruns the same install for noop proof
- runs `inspect-state`
- collects remote state and logs locally

## Local validation

Quick checks:

```bash
pytest -q
ruff check .
mypy .
```

Focused modules that matter most for the current stack:

```bash
pytest tests/unit/test_openclaw_pack.py -q
pytest tests/unit/test_nextcloud_pack.py -q
pytest tests/unit/test_nexa_runtime.py -q
pytest tests/integration/test_openclaw_pack.py -q
pytest tests/integration/test_nextcloud_pack.py -q
pytest tests/test_cli.py -q
```

## Operator notes

- This is still a **fresh-host** workflow, not a general migration framework.
- Docker can be bootstrap-remediated by the wizard on supported Ubuntu hosts.
- The chosen state directory stores wizard metadata and the generated env file, not the Docker volumes themselves.
- The ownership ledger is the uninstall authority.
- OpenClaw/Nexa/Telly behavior is now wizard-managed, not just manually drifted on one VPS.

## Current caveats

- Dokploy itself is not yet protected by Cloudflare Access because the wizard still needs a safe machine-auth/control path.
- Nexa features are env-gated, not universal defaults. For your deployment that is fine, because `.install.env` already carries the required `OPENCLAW_NEXA_*` values.
- Some channel/runtime behavior still depends on the upstream OpenClaw image, so operational behavior can evolve as that image evolves.

## LiteLLM core gateway

LiteLLM is always installed as core infrastructure. It is not a pack you opt into, and it runs as a shared-core service alongside Postgres and Redis. Every AI consumer in the stack, including My Farm Advisor, OpenClaw, and Coder templates, routes model calls through LiteLLM using a per-consumer virtual key.

### Flat env inputs

The operator env file stays flat. `.install.env` contains only key=value pairs. The wizard generates the nested LiteLLM `config.yaml` internally during deployment. You do not edit raw LiteLLM proxy config by hand.

### Model allowlist and default order

The generated LiteLLM config enforces a strict precedence:

1. `local/unsloth-active` — the local vLLM or Tailnet endpoint, when `LITELLM_LOCAL_BASE_URL` is configured
2. OpenCode Go wildcard — a single `openai/*` route that covers the OpenCode Go provider fleet
3. Explicit OpenRouter aliases — each alias is declared individually with `alias=target-model` pairs in `LITELLM_OPENROUTER_MODELS`

OpenRouter wildcard routes are not allowed. The config renderer rejects `openrouter/*` or broad `*` aliases.

### Virtual keys

The wizard auto-generates stable virtual keys for each consumer:

- My Farm Advisor
- OpenClaw
- Coder Hermes
- Coder K-Dense

These keys are generated once and reused across reruns and modify operations. They are stored in the wizard state directory, not written back into `.install.env`. If you need to rotate a key, that is a future operator action, not something that happens silently on reinstall.

### Admin access

LiteLLM management UI and API are reachable at `litellm.<root-domain>`. This hostname is protected by Cloudflare Access before any public DNS or tunnel routing is created. Internal consumers use the Docker network URL `http://<stack-name>-shared-litellm:4000`, not the public admin hostname.

### Migration from direct provider envs

If you previously set direct provider keys like `MY_FARM_ADVISOR_OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`, those values are still accepted as upstream inputs for LiteLLM config generation. After cutover, consumers no longer receive those raw upstream keys. Instead, each consumer gets its own LiteLLM virtual key and the internal base URL. Upstream secrets terminate at the LiteLLM proxy.

### Validation

Quick checks:

```bash
pytest -q
ruff check .
mypy .
```

## My Farm Advisor operator reference

### Required env keys

My Farm Advisor is enabled with `ENABLE_MY_FARM_ADVISOR=true`. When it is enabled, you must provide at least one model provider path.

| Key | Example | Notes |
|---|---|---|
| `ENABLE_MY_FARM_ADVISOR` | `true` | Pack flag |
| `MY_FARM_ADVISOR_CHANNELS` | `telegram` | Comma-separated; `telegram` and `matrix` are supported. Matrix requires the Matrix pack. |
| `MY_FARM_ADVISOR_SUBDOMAIN` | `farm` | Defaults to `farm` |
| `MY_FARM_ADVISOR_GATEWAY_PASSWORD` | `changeme` | Browser/control UI password. `ADVISOR_GATEWAY_PASSWORD` is a shared fallback for both advisors. |
| **Provider (at least one)** | | |
| `MY_FARM_ADVISOR_OPENROUTER_API_KEY` | `<your-openrouter-key>` | Farm-only OpenRouter key |
| `MY_FARM_ADVISOR_NVIDIA_API_KEY` | `<your-nvidia-key>` | Farm-only NVIDIA key |
| `ANTHROPIC_API_KEY` | `<your-anthropic-key>` | Shared across packs |
| `AI_DEFAULT_API_KEY` + `AI_DEFAULT_BASE_URL` | `sk-...` + `https://...` | Shared fallback pair; both must be present to count as a valid provider path |

### Optional env keys

| Key | Example | Notes |
|---|---|---|
| `MY_FARM_ADVISOR_REPLICAS` | `1` | Defaults to 1 |
| `MY_FARM_ADVISOR_PRIMARY_MODEL` | `openrouter/openrouter/hunter-alpha` | |
| `MY_FARM_ADVISOR_FALLBACK_MODELS` | `openrouter/openrouter/healer-alpha,openrouter/nvidia/...` | Comma-separated |
| `MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN` | `...` | Main farm Telegram bot |
| `MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID` | `12345678` | DM allowlist owner |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | |
| `TZ` | `America/Chicago` | Container timezone |

### Feature-gated env keys

These keys are accepted only when the `my-farm-advisor` pack is enabled, but they are not required.

**Field Operations and Data Pipeline Telegram bots**

| Key | Purpose |
|---|---|
| `TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN` | Field ops bot |
| `TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE` | Pairing code |
| `TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS` | Allowlist |
| `TELEGRAM_DATA_PIPELINE_BOT_TOKEN` | Data pipeline bot |
| `TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE` | Pairing code |
| `TELEGRAM_DATA_PIPELINE_ALLOWED_USERS` | Allowlist |
| `TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS` | Secondary allowlist |
| `TELEGRAM_ALLOWED_USERS` | General allowlist |
| `OPENCLAW_TELEGRAM_GROUP_POLICY` | `allowlist` or `open` |

**R2 / data pipeline persistence**

R2 is only mounted when all of the following are present and `WORKSPACE_DATA_R2_RCLONE_MOUNT=1`:

| Key | Example |
|---|---|
| `R2_BUCKET_NAME` | `my-farm-advisor` |
| `R2_ENDPOINT` | `https://your-account-id.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | `...` |
| `R2_SECRET_ACCESS_KEY` | `...` |
| `CF_ACCOUNT_ID` | `...` |
| `DATA_MODE` | `volume` |
| `WORKSPACE_DATA_R2_RCLONE_MOUNT` | `0` or `1` |
| `WORKSPACE_DATA_R2_PREFIX` | `workspace/data` |

When R2 is not fully configured, the container explicitly sets `OPENCLAW_SYNC_SKILLS_ON_START=0` so local skill sync does not run.

**Skill and bootstrap control**

| Key | Default | Purpose |
|---|---|---|
| `OPENCLAW_SYNC_SKILLS_ON_START` | `0` when R2 is off | Enable skill sync on start |
| `OPENCLAW_SYNC_SKILLS_OVERWRITE` | `1` | Overwrite existing skills |
| `OPENCLAW_FORCE_SKILL_SYNC` | `0` | Force a one-time sync |
| `OPENCLAW_BOOTSTRAP_REFRESH` | `0` | Re-seed config on next start |
| `OPENCLAW_MEMORY_SEARCH_ENABLED` | `0` | Enable memory search |

### Nextcloud workspace mounts

When Nextcloud is enabled alongside My Farm Advisor, the wizard creates two external storage mounts:

| Mount name | Purpose |
|---|---|
| `/Nexa Farm` | Field ops workspace (`/data/workspace`) |
| `/Nexa Farm Data Pipeline` | Data pipeline workspace (`/data/workspace-data-pipeline`) |

For reference, OpenClaw uses `/Nexa Claw` (new installs) or `/OpenClaw` (legacy).

### Side-by-side with OpenClaw

My Farm Advisor and OpenClaw can be enabled in the same install. They use separate Dokploy compose apps, separate hostnames, and separate volumes. Shared provider keys like `AI_DEFAULT_API_KEY` and `ANTHROPIC_API_KEY` are reused by both advisors unless you override them with pack-specific keys.

### Migrating from the old Coolify-era `.env`

If you have an existing My Farm Advisor deployment from the Coolify era, the key names have changed. The wizard now uses prefixed keys so farm settings do not collide with OpenClaw.

| Old Coolify key | New wizard key |
|---|---|
| `OPENROUTER_API_KEY` | `MY_FARM_ADVISOR_OPENROUTER_API_KEY` (or shared `AI_DEFAULT_API_KEY`) |
| `NVIDIA_API_KEY` | `MY_FARM_ADVISOR_NVIDIA_API_KEY` |
| `PRIMARY_MODEL` | `MY_FARM_ADVISOR_PRIMARY_MODEL` |
| `FALLBACK_MODELS` | `MY_FARM_ADVISOR_FALLBACK_MODELS` |
| `TELEGRAM_BOT_TOKEN` | `MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN` |
| `TELEGRAM_ACCOUNT_ID` | Not mapped; use `MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID` instead |

Keys that kept the same name:

- `ANTHROPIC_API_KEY`
- `NVIDIA_BASE_URL`
- `TELEGRAM_FIELD_OPERATIONS_BOT_TOKEN`
- `TELEGRAM_FIELD_OPERATIONS_BOT_PAIRING_CODE`
- `TELEGRAM_FIELD_OPERATIONS_ALLOWED_USERS`
- `TELEGRAM_DATA_PIPELINE_BOT_TOKEN`
- `TELEGRAM_DATA_PIPELINE_BOT_PAIRING_CODE`
- `TELEGRAM_DATA_PIPELINE_ALLOWED_USERS`
- `TELEGRAM_DATA_PIPELINE_BOT_ALLOWED_USERS`
- `TELEGRAM_ALLOWED_USERS`
- `OPENCLAW_TELEGRAM_GROUP_POLICY`
- `R2_BUCKET_NAME`
- `R2_ENDPOINT`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `CF_ACCOUNT_ID`
- `DATA_MODE`
- `WORKSPACE_DATA_R2_RCLONE_MOUNT`
- `WORKSPACE_DATA_R2_PREFIX`
- `OPENCLAW_SYNC_SKILLS_ON_START`
- `OPENCLAW_SYNC_SKILLS_OVERWRITE`
- `OPENCLAW_FORCE_SKILL_SYNC`
- `OPENCLAW_BOOTSTRAP_REFRESH`
- `OPENCLAW_MEMORY_SEARCH_ENABLED`
- `TZ`

### Breaking changes from the Coolify era

1. **No more `OPENCLAW_GATEWAY_TOKEN` for farm**. The wizard manages gateway tokens and passwords through `ADVISOR_GATEWAY_PASSWORD` or `MY_FARM_ADVISOR_GATEWAY_PASSWORD`.
2. **No `CLOUDFLARE_TUNNEL_TOKEN` in env**. The wizard wires ingress through its own Cloudflare Tunnel phase.
3. **No `OPENCLAW_PUBLIC_HOSTNAME` in env**. The hostname is derived from `MY_FARM_ADVISOR_SUBDOMAIN` and `ROOT_DOMAIN`.
4. **Provider keys are prefixed**. Unprefixed `OPENROUTER_API_KEY` and `NVIDIA_API_KEY` are no longer read for farm; use the `MY_FARM_ADVISOR_*` versions or the shared `AI_DEFAULT_*` pair.
5. **R2 is opt-in and gated**. Partial R2 config is ignored; all five credential fields plus `WORKSPACE_DATA_R2_RCLONE_MOUNT=1` must be present for the mount to activate.

## Project layout

- `src/dokploy_wizard/cli.py` — lifecycle entrypoint and backend construction
- `src/dokploy_wizard/networking/` — Cloudflare tunnel/DNS/Access planning
- `src/dokploy_wizard/tailscale/` — host-level Tailscale phase
- `src/dokploy_wizard/dokploy/` — Dokploy-backed deployment backends
- `src/dokploy_wizard/packs/` — pack models and reconcilers
- `templates/` — deployment templates, including Coder template assets
- `tests/` — unit, integration, and lifecycle coverage

## Summary

Dokploy Wizard now installs a real, stateful self-hosted stack, not just a set of placeholders. It bootstraps Dokploy, wires ingress and auth, deploys application packs, embeds OpenClaw-facing agents like Nexa and Telly, and supports fresh-host reruns with state-aware lifecycle behavior.

The current repo is built around a real fresh-VPS proof workflow and is intended to be the baseline for repeatable full rebuilds.
