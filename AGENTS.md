# Dokploy Wizard — Agent Operational Guide

## Project Overview

Dokploy Wizard is a Python-first installer that stands up a real self-hosted stack on a single fresh Ubuntu VPS. It deploys Dokploy as the control plane, wires Cloudflare Tunnel for public ingress, optionally enables Cloudflare Access OTP and Tailscale host access, and installs opinionated application packs including Nextcloud, OpenClaw, My Farm Advisor, SeaweedFS, and Coder. The wizard supports full lifecycle operations: install, modify, rerun, inspect-state, and uninstall.

This guide covers day-to-day commands, secret handling, testing, and safety rules. For bin-level wrapper details, see [bin/AGENTS.md](bin/AGENTS.md).

## Local Commands

Use these commands from the repo root on a machine that already has the code and an env file.

### Guided first-run install

```bash
./bin/dokploy-wizard install
```

### Env-file install (non-interactive)

```bash
./bin/dokploy-wizard install --env-file ./.install.env --non-interactive
```

### Inspect state

```bash
./bin/dokploy-wizard inspect-state --env-file ./.install.env --state-dir .dokploy-wizard-state
```

### Modify / rerun

```bash
./bin/dokploy-wizard modify --env-file ./.install.env --non-interactive
```

### Uninstall

```bash
./bin/dokploy-wizard uninstall --retain-data --non-interactive --confirm-file fixtures/retain.confirm
./bin/dokploy-wizard uninstall --destroy-data --non-interactive --confirm-file fixtures/destroy.confirm
```

## Remote Deployment

To deploy to a fresh remote VPS, use the remote helper:

```bash
./bin/dokploy-wizard-remote install \
  --host <host> \
  --password <password> \
  --env-file ./.install.env
```

### Agent runtime guidance (timeouts)

Remote `proof`, `install`, and `modify` runs are long. A cold first install on a
fresh VPS routinely takes 10-20 minutes (Docker image pulls dominate) and can
take up to 60 minutes on slow links. Agents must NOT cancel or retry these
commands on a short tool timeout: pass a timeout of at least 3,600,000 ms, or
run the command in the background and wait for the completion notification. A
`still running ... waiting for output` progress line means the wizard is
healthy, not stuck. Re-running an interrupted install is safe but wasteful; if
a run failed at the Dokploy bootstrap health check, remove the half-built
bootstrap on the host first (`docker service rm dokploy dokploy-postgres`)
before retrying, or the preflight port check will reject port 3000.

What the helper does:

1. Packages the repo.
2. Uploads the repo plus `.install.env` to the remote host.
3. Runs the wizard install non-interactively.
4. Reruns the same install for a noop proof.
5. Runs `inspect-state`.
6. Collects remote state and logs locally.

The helper targets a single fresh host. It uses password authentication over SSH. It does not support authentication via stored private keys or unattended login, and it is not designed for orchestrating more than one host at a time.

### Proof command (verification-first)

The preferred operator path for fresh-VPS validation is `proof`:

```bash
./bin/dokploy-wizard-remote proof \
  --host <host> \
  --password <password> \
  --env-file ./.install.env
```

What the proof flow does:

1. Packages the repo.
2. Uploads the repo plus `.install.env` to the remote host.
3. Runs the wizard install non-interactively.
4. Runs service verification for all enabled services.
5. Runs `inspect-state`.
6. Collects remote state and logs locally.

By default, proof does **not** run a second install pass. It is verification-first: install once, verify services, inspect state. Use `--strict-idempotency` when you want an explicit double-install check that asserts unchanged healthy services stay no-op.

```bash
./bin/dokploy-wizard-remote proof \
  --host <host> \
  --password <password> \
  --env-file ./.install.env \
  --strict-idempotency
```

## .install.env

`.install.env` is the working operator env file. It stays at repo root and contains flat `key=value` pairs for credentials, domains, pack flags, and model provider settings.

Important:

- Keep `.install.env` at mode `0600` because it contains passwords and API keys.
- The remote helper copies it explicitly to the remote host during upload.
- Runtime-only values (internal sidecar URLs, generated Dokploy API keys) are synthesized later during deployment and stored in the state directory, not written back into `.install.env`.

## Secrets

Treat all credentials as sensitive. Follow these rules:

- Redact secrets in logs, screenshots, and pasted output. Replace passwords, tokens, and keys with `<REDACTED>`.
- Keep `.install.env` at `chmod 0600`.
- Do not log passwords or raw API keys in CI output, issue comments, or chat transcripts.
- Wizard-generated values (SeaweedFS keys, gateway passwords, Dokploy API keys) are stored in the state directory. Rotate them through operator action, not silent reinstall.

## Testing

Run the full test suite before committing changes:

```bash
pytest -q
ruff check .
mypy .
```

Focused modules that matter most:

```bash
pytest tests/unit/test_openclaw_pack.py -q
pytest tests/unit/test_nextcloud_pack.py -q
pytest tests/unit/test_nexa_runtime.py -q
pytest tests/integration/test_openclaw_pack.py -q
pytest tests/integration/test_nextcloud_pack.py -q
pytest tests/test_cli.py -q
```

## Safety

The wizard performs real destructive operations on real hosts. Follow these safety practices:

- Always use `--confirm-file` for uninstall. Never skip confirmation.
- Run `inspect-state` before modify operations to understand current state.
- The ownership ledger is the uninstall authority. Do not manually remove Docker volumes or networks outside the wizard.
- This is a fresh-host workflow, not a migration framework. Do not run it against hosts with existing unmanaged Dokploy or Docker workloads unless you understand the collision risk.
- Test changes on a disposable VPS before running against production targets.

## See Also

- [bin/AGENTS.md](bin/AGENTS.md) — bin shim pattern, wrapper naming, and remote helper conventions.
