# Dokploy Wizard — bin/ Conventions

## Bin Shim Pattern

Every executable in `bin/` is a thin bash shim that sets `PYTHONPATH` and delegates to the matching Python module. This keeps the repo runnable without installation and avoids global PATH mutations.

Example from `bin/dokploy-wizard`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

exec python3 -m dokploy_wizard "$@"
```

The shim computes `SCRIPT_DIR`, resolves `REPO_ROOT`, prepends `src/` to `PYTHONPATH`, and then `exec`s the module so the Python process replaces the shell.

## Wrapper Naming

- `bin/dokploy-wizard` wraps `dokploy_wizard` (the main CLI).
- `bin/dokploy-wizard-remote` wraps `dokploy_wizard.remote`.

Name new wrappers to match the module path they invoke. Prefer hyphens in the filename and dots in the module path.

## Remote Helper Usage

The `bin/dokploy-wizard-remote` wrapper is the standard entrypoint for fresh-VPS deploys. It accepts the same arguments as the underlying Python module:

```bash
./bin/dokploy-wizard-remote install \
  --host <host> \
  --password <password> \
  --env-file ./.install.env
```

This helper packages the repo, uploads it plus the env file, runs install, reruns for noop proof, runs `inspect-state`, and collects artifacts locally.

## No-Secret Logging

Bin wrappers themselves do not log secrets. They only set `PYTHONPATH` and exec Python. Any logging of credentials happens inside Python, where the same rules apply:

- Redact passwords, tokens, and keys before writing to stdout, stderr, or files.
- Do not echo command lines that contain literal passwords when the output is visible to operators or CI.
- Treat any env var that ends in `_PASSWORD`, `_TOKEN`, `_SECRET`, or `_API_KEY` as sensitive.
