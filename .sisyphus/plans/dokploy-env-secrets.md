# Dokploy Environment Secret Scoping Refactor

## TL;DR
> **Summary**: Refactor Dokploy Wizard so generated compose YAML is non-secret topology only, while actual secret/env values are reconciled through Dokploy compose/service environment configuration before first deploy. The primary proof is a complete fresh-VPS install where all wizard-managed services receive only the variables they need.
> **Deliverables**:
> - Central Dokploy env-spec and reconciliation abstraction.
> - TDD leakage/payload/no-op/redaction tests.
> - All wizard-managed compose renderers migrated to safe placeholders plus least-privilege env specs.
> - Fresh-VPS proof validation with collected artifacts checked for raw secret absence.
> **Effort**: Large
> **Parallel**: YES - 4 waves
> **Critical Path**: Task 1 → Task 2 → Tasks 3-8 → Task 9 → Task 10

## Context

### Original Request
Move all secrets and env vars currently written into Dokploy compose files into the proper Dokploy environment configuration for each project/service, following principle of least privilege. Compose files should reference env vars rather than contain literal secret values.

### Interview Summary
- Scope: all wizard-managed Dokploy services, including shared-core, cloudflared, user-facing app packs, and infra packs.
- Primary success path: complete fresh-VPS proof install sets all env vars correctly through Dokploy from first deployment.
- Test posture: TDD first.
- CI workflow: out of scope.
- Secret rotation and `.install.env` format changes: out of scope.

### Metis Review (gaps addressed)
- Added mandatory value-classification and ownership metadata.
- Locked env update ordering: create resource → reconcile env → submit safe compose → deploy → verify.
- Locked no-op hash behavior: include safe compose plus env spec metadata/fingerprint, never raw env values.
- Added deleted/renamed env handling: wizard-owned stale vars may be removed only when owned by the same service/spec; non-wizard env must be preserved.
- Added repository-wide leakage tests covering all compose scalar values, command strings, labels, healthchecks, URLs, state, logs, and remote artifacts.

## Work Objectives

### Core Objective
Make generated compose YAML safe to inspect, log, hash, and collect by moving raw env values/secrets into Dokploy environment payloads and exposing each variable only to services that explicitly need it.

### Deliverables
- `DokployEnvSpec` / `DokployEnvVar` model with scope, owner service, target services, sensitivity, source, placeholder, and fingerprint fields.
- `DokployEnvReconciler` integrated into both API-key and bootstrap-auth Dokploy paths.
- Renderer contract that returns safe compose YAML plus structured env specs.
- Migrated renderers for all wizard-managed Dokploy services.
- TDD tests and fresh-VPS proof verification.

### Definition of Done (verifiable conditions with commands)
- `pytest tests/unit/test_compose_secret_leakage.py -q` passes and proves no raw known secrets appear in rendered compose YAML.
- `pytest tests/unit/test_dokploy_env_reconciliation.py -q` passes and proves env payload values go through Dokploy env APIs, not compose YAML.
- `pytest tests/unit/test_compose_noop.py -q` passes and proves unchanged safe compose/env metadata remains no-op while env spec changes trigger updates without hashing raw values.
- `pytest tests/unit/test_verification.py tests/test_cli.py tests/test_remote_cli.py tests/test_remote_transport.py -q` passes and proves no new secret leak paths.
- `pytest -q && ruff check . && mypy .` passes.
- Fresh VPS proof succeeds:
  ```bash
  ./bin/dokploy-wizard-remote proof \
    --host <fresh-test-host> \
    --password <REDACTED> \
    --env-file ./.install.env
  ```

### Must Have
- Compose YAML contains placeholders only for secrets/required env: `${VAR:?VAR is required}`.
- Compose service `environment:` mappings explicitly list the variables each service receives.
- Actual values are sent through Dokploy compose/service env payloads before first deploy.
- Variable names are deterministic and service-prefixed unless intentionally shared.
- Raw secrets never appear in compose, inspect-state, logs, state JSON, no-op hash input/output, API diagnostics, or remote proof artifacts.

### Must NOT Have
- No secret rotation.
- No `.install.env` format change.
- No CI workflow.
- No global all-secrets env bag.
- No blanket `env_file: .env` as the default injection strategy.
- No moving secrets into labels, commands, healthchecks, image tags, URLs, or volume names.
- No deletion of non-wizard-owned Dokploy env entries.

## Verification Strategy
> ZERO HUMAN INTERVENTION - all verification is agent-executed.
- Test decision: TDD first with pytest.
- QA policy: Every task has agent-executed scenarios.
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.{ext}`

## Execution Strategy

### Parallel Execution Waves
> Target: 5-8 tasks per wave. <3 per wave (except final) = under-splitting.
> Extract shared dependencies as Wave-1 tasks for max parallelism.

Wave 1: Tasks 1-2 foundation tests and central model/reconciler.
Wave 2: Tasks 3-8 renderer migrations in parallel by service group.
Wave 3: Tasks 9-10 lifecycle/no-op/redaction/fresh proof harness.
Wave 4: Final verification agents F1-F4.

### Dependency Matrix (full, all tasks)
- Task 1 blocks Tasks 2-10.
- Task 2 blocks Tasks 3-10.
- Tasks 3-8 block Task 9.
- Task 9 blocks Task 10.
- Task 10 blocks Final Verification Wave.

### Agent Dispatch Summary
- Wave 1 → 2 tasks → `deep`, `unspecified-high`
- Wave 2 → 6 tasks → `deep`/`unspecified-high` per renderer group
- Wave 3 → 2 tasks → `deep`, `unspecified-high`
- Wave 4 → 4 review tasks → oracle/code QA/manual QA/scope fidelity

## TODOs
> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [x] 1. Add TDD Secret Leakage and Env Payload Contract Tests

  **What to do**: Create failing tests before implementation. Add `tests/unit/test_compose_secret_leakage.py` to render representative desired states for all enabled packs using fixtures from `fixtures/` and `tests/helpers/root_install_env.py`, then assert raw known secrets are absent from every compose scalar and present only in structured env specs. Add `tests/unit/test_dokploy_env_reconciliation.py` to assert Dokploy payload ordering and least-privilege target services. Update existing pack tests only to call shared fixtures, not to make them pass yet.
  **Must NOT do**: Do not weaken existing substring assertions by simply deleting them without replacing with env-spec assertions.

  **Recommended Agent Profile**:
  - Category: `deep` - Reason: needs repository-wide test design and security invariants.
  - Skills: [`fullstack-dev-skills:test-master`] - Use if available for pytest strategy.
  - Omitted: [`cso`] - Security principles are already captured; this is test implementation planning/execution.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 2-10 | Blocked By: none

  **References**:
  - Pattern: `tests/unit/test_openclaw_pack.py` - pack render assertions.
  - Pattern: `tests/unit/test_nextcloud_pack.py` - compose/backend assertions.
  - Pattern: `tests/unit/test_litellm_shared_core.py` - existing env reference assertions.
  - Pattern: `tests/unit/test_dokploy_client.py` - Dokploy API payload style.
  - Fixture: `tests/helpers/root_install_env.py` - representative in-memory env with secrets.
  - Fixture: `fixtures/full.env`, `fixtures/nextcloud.env`, `fixtures/openclaw-telegram.env`, `fixtures/moodle-docuseal.env`.

  **Acceptance Criteria**:
  - [ ] `pytest tests/unit/test_compose_secret_leakage.py -q` fails before implementation for current raw compose secrets.
  - [ ] `pytest tests/unit/test_dokploy_env_reconciliation.py -q` fails before implementation because env specs/reconciler do not exist.
  - [ ] Tests include happy path and failure path: unmatched placeholder, raw secret leak, and unrelated service receiving a secret.

  **QA Scenarios**:
  ```
  Scenario: TDD failure proves current leak
    Tool: Bash
    Steps: Run `pytest tests/unit/test_compose_secret_leakage.py -q` immediately after adding tests.
    Expected: Fails with at least one assertion naming a renderer and redacted secret key name, not raw value.
    Evidence: .sisyphus/evidence/task-1-secret-leakage-failing.txt

  Scenario: Least-privilege failure proves missing env specs
    Tool: Bash
    Steps: Run `pytest tests/unit/test_dokploy_env_reconciliation.py -q` immediately after adding tests.
    Expected: Fails because no central env spec/reconciler exists; failure output contains no raw secret values.
    Evidence: .sisyphus/evidence/task-1-env-reconciler-failing.txt
  ```

  **Commit**: YES | Message: `test(security): capture dokploy env secret contract` | Files: `tests/unit/test_compose_secret_leakage.py`, `tests/unit/test_dokploy_env_reconciliation.py`, shared test helpers if needed

- [x] 2. Implement Central Dokploy Env Spec and Reconciler

  **What to do**: Add a central typed model in `src/dokploy_wizard/dokploy/` for env variables and rendered compose artifacts. Required fields: variable name, value, sensitivity, source (`operator-input`, `wizard-generated`, `runtime-derived`), owner component, Dokploy scope (`compose` by default), target service names, placeholder string, required/optional flag, redacted fingerprint, and ownership marker. Add a reconciler that serializes service/compose env text, preserves non-wizard entries, removes stale wizard-owned entries only for the same owner, validates placeholders are matched, and uses `compose.create` / `compose.update` with the `env` field as the canonical Dokploy API path. Do not use project/environment-level env for secrets in this refactor. Do not use `compose.saveEnvironment` unless `compose.update env` is proven unusable by a failing test; if that happens, implement only inside the central reconciler and record the reason in code comments and tests.
  **Must NOT do**: Do not let individual pack modules call Dokploy env APIs directly. Do not store raw env values in compose hash state.

  **Recommended Agent Profile**:
  - Category: `deep` - Reason: central architecture contract and API behavior.
  - Skills: [`fullstack-dev-skills:python-pro`] - Type-safe Python model/refactor.
  - Omitted: [`cloudflare`] - Not a Cloudflare platform task.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 3-10 | Blocked By: 1

  **References**:
  - API client: `src/dokploy_wizard/dokploy/client.py` - create/update compose and project APIs.
  - Bootstrap fallback: `src/dokploy_wizard/dokploy/bootstrap_auth.py` - session-auth compose create/update path.
  - No-op: `src/dokploy_wizard/dokploy/compose_noop.py` - normalized hash behavior.
  - State: `src/dokploy_wizard/state/models.py`, `src/dokploy_wizard/state/store.py` - state contracts and `0600` generated key storage.
  - External: `https://docs.dokploy.com/docs/api/reference-compose` - compose update env support.
  - External: `https://docs.dokploy.com/docs/core/docker-compose` - `.env` is not injected without explicit mappings.

  **Acceptance Criteria**:
  - [ ] `pytest tests/unit/test_dokploy_env_reconciliation.py -q` passes.
  - [ ] Reconciler validates every `${VAR:?message}` placeholder has exactly one desired env spec unless explicitly optional.
  - [ ] Reconciler serializes env payload without logging raw values and preserves non-wizard entries.
  - [ ] `mypy .` passes with no new unjustified ignores.

  **QA Scenarios**:
  ```
  Scenario: Env reconciled before deploy
    Tool: Bash
    Steps: Run `pytest tests/unit/test_dokploy_env_reconciliation.py -q`.
    Expected: Fake client records create/find, env reconcile, safe compose update, deploy in that order.
    Evidence: .sisyphus/evidence/task-2-env-order.txt

  Scenario: Missing placeholder fails closed
    Tool: Bash
    Steps: Run the test case that renders `${MISSING_SECRET:?MISSING_SECRET is required}` without env spec.
    Expected: Raises a typed validation error before deploy; error names `MISSING_SECRET` only, no raw values.
    Evidence: .sisyphus/evidence/task-2-missing-placeholder.txt
  ```

  **Commit**: YES | Message: `feat(dokploy): add env reconciliation contract` | Files: `src/dokploy_wizard/dokploy/*`, `tests/unit/test_dokploy_env_reconciliation.py`

- [x] 3. Migrate Shared Core and LiteLLM Env Handling

  **What to do**: Update `src/dokploy_wizard/dokploy/shared_core.py` so Postgres, Redis, mail relay, and LiteLLM compose output contains only explicit placeholder mappings. Move LiteLLM master/salt keys, upstream provider keys, virtual keys, Postgres/Redis passwords, and generated config-sensitive values into env specs. For generated LiteLLM config, keep secret references as environment placeholders where the image/config supports env expansion; if a secret must be written into a config file, classify it as a documented exception and keep the config file out of compose/log/hash output with redaction tests.
  **Must NOT do**: Do not inline upstream provider keys into compose or generated compose-visible config. Do not expose LiteLLM upstream keys to non-LiteLLM consumers.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: security-sensitive renderer migration.
  - Skills: [`fullstack-dev-skills:python-pro`] - Python refactor and tests.
  - Omitted: [`workers-best-practices`] - Not a Workers task.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 9 | Blocked By: 2

  **References**:
  - Renderer: `src/dokploy_wizard/dokploy/shared_core.py`.
  - LiteLLM config: `src/dokploy_wizard/litellm/config_renderer.py`.
  - Tests: `tests/unit/test_litellm_shared_core.py`, `tests/unit/test_compose_secret_leakage.py`.
  - State keys: `src/dokploy_wizard/state/models.py`, `src/dokploy_wizard/state/store.py`.

  **Acceptance Criteria**:
  - [ ] `pytest tests/unit/test_litellm_shared_core.py tests/unit/test_compose_secret_leakage.py -q` passes for shared-core cases.
  - [ ] Shared-core compose has explicit per-service `environment:` mappings and no raw provider/master/salt/DB/Redis secret values.
  - [ ] LiteLLM receives upstream provider keys only through its target service env spec.

  **QA Scenarios**:
  ```
  Scenario: Shared core safe compose
    Tool: Bash
    Steps: Run `pytest tests/unit/test_litellm_shared_core.py -q`.
    Expected: Compose contains `${SHARED_CORE_POSTGRES_PASSWORD:?...}` style refs and no raw fixture secrets.
    Evidence: .sisyphus/evidence/task-3-shared-core.txt

  Scenario: Provider key isolation
    Tool: Bash
    Steps: Run the leakage test filtering LiteLLM/OpenRouter fixture secrets.
    Expected: Only LiteLLM env spec owns upstream provider keys; no app service target receives them.
    Evidence: .sisyphus/evidence/task-3-provider-isolation.txt
  ```

  **Commit**: YES | Message: `refactor(shared-core): move secrets to dokploy env specs` | Files: `src/dokploy_wizard/dokploy/shared_core.py`, `src/dokploy_wizard/litellm/config_renderer.py`, relevant tests

- [x] 4. Migrate Nextcloud and OnlyOffice Env Handling

  **What to do**: Update `src/dokploy_wizard/dokploy/nextcloud.py` to move `NEXTCLOUD_ADMIN_PASSWORD`, DB credentials, Redis password, OnlyOffice JWT, service account values, and trusted-domain/runtime env values into least-privilege env specs. Nextcloud receives only Nextcloud-required vars; OnlyOffice receives only OnlyOffice JWT and its own config; Redis/Postgres sidecars receive only their own passwords where applicable.
  **Must NOT do**: Do not leak admin password/JWT in compose healthchecks, commands, labels, or generated previews.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: multi-service pack with shared secrets.
  - Skills: [`fullstack-dev-skills:python-pro`] - Python renderer/tests.
  - Omitted: [`playwright`] - Browser QA is in final proof, not this unit task.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 9 | Blocked By: 2

  **References**:
  - Renderer: `src/dokploy_wizard/dokploy/nextcloud.py`.
  - Tests: `tests/unit/test_nextcloud_pack.py`, `tests/integration/test_nextcloud_pack.py`.
  - Fixture: `fixtures/nextcloud.env`.

  **Acceptance Criteria**:
  - [ ] `pytest tests/unit/test_nextcloud_pack.py tests/integration/test_nextcloud_pack.py tests/unit/test_compose_secret_leakage.py -q` passes for Nextcloud cases.
  - [ ] Compose contains placeholders for admin password, DB password, Redis password, and JWT; raw values appear only in env specs.
  - [ ] Each service target receives only required variables.

  **QA Scenarios**:
  ```
  Scenario: Nextcloud install render
    Tool: Bash
    Steps: Run `pytest tests/unit/test_nextcloud_pack.py -q`.
    Expected: Nextcloud/OnlyOffice render assertions pass with placeholders and matching env specs.
    Evidence: .sisyphus/evidence/task-4-nextcloud-render.txt

  Scenario: Missing OnlyOffice JWT fails before deploy
    Tool: Bash
    Steps: Run failure-path test omitting OnlyOffice JWT env spec while compose references it.
    Expected: Typed validation failure before Dokploy deploy; no raw password/JWT in output.
    Evidence: .sisyphus/evidence/task-4-nextcloud-missing-jwt.txt
  ```

  **Commit**: YES | Message: `refactor(nextcloud): scope secrets through dokploy env` | Files: `src/dokploy_wizard/dokploy/nextcloud.py`, relevant tests

- [x] 5. Migrate OpenClaw, Nexa, Telly, and My Farm Advisor Env Handling

  **What to do**: Update `src/dokploy_wizard/dokploy/openclaw.py` to move gateway passwords/tokens, LiteLLM virtual keys, Telegram tokens, R2 credentials, Nexa Talk/WebDAV/OnlyOffice values, Mem0/Qdrant API keys, planner/model keys, and farm-specific values into service-specific env specs. Keep farm-only values out of OpenClaw and OpenClaw-only values out of Farm. Preserve generated runtime contract files while redacting secret presence/source only.
  **Must NOT do**: Do not pass raw upstream provider keys directly to OpenClaw/Farm if the intended post-cutover path is LiteLLM virtual keys. Do not collapse Farm and OpenClaw env bags.

  **Recommended Agent Profile**:
  - Category: `deep` - Reason: largest, most security-sensitive pack and multiple sidecars.
  - Skills: [`fullstack-dev-skills:python-pro`] - Python renderer/tests.
  - Omitted: [`research-lookup`] - External research complete.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 9 | Blocked By: 2

  **References**:
  - Renderer: `src/dokploy_wizard/dokploy/openclaw.py`.
  - Tests: `tests/unit/test_openclaw_pack.py`, `tests/integration/test_openclaw_pack.py`, `tests/unit/test_nexa_runtime.py`.
  - Fixtures: `fixtures/openclaw-telegram.env`, `fixtures/full.env`.

  **Acceptance Criteria**:
  - [ ] `pytest tests/unit/test_openclaw_pack.py tests/unit/test_nexa_runtime.py tests/integration/test_openclaw_pack.py tests/unit/test_compose_secret_leakage.py -q` passes for OpenClaw/Farm cases.
  - [ ] Farm env specs include farm-only Telegram/R2/model values; OpenClaw env specs exclude them unless explicitly shared.
  - [ ] Nexa sidecar env specs are separate from browser-facing OpenClaw service env specs.

  **QA Scenarios**:
  ```
  Scenario: Advisor least privilege
    Tool: Bash
    Steps: Run `pytest tests/unit/test_openclaw_pack.py -q`.
    Expected: Tests prove Farm-only secrets are not targeted to OpenClaw and OpenClaw-only secrets are not targeted to Farm.
    Evidence: .sisyphus/evidence/task-5-advisor-isolation.txt

  Scenario: R2 optional secrets
    Tool: Bash
    Steps: Run leakage tests with partial R2 fixture values.
    Expected: Partial R2 remains disabled/fail-safe and no R2 secret appears in compose or unrelated env specs.
    Evidence: .sisyphus/evidence/task-5-r2-optional.txt
  ```

  **Commit**: YES | Message: `refactor(advisors): isolate secrets in dokploy env specs` | Files: `src/dokploy_wizard/dokploy/openclaw.py`, relevant tests

- [x] 6. Migrate Storage, Tunnel, and Infra Pack Env Handling

  **What to do**: Migrate `seaweedfs.py`, `cloudflared.py`, `docuseal.py`, `moodle.py`, and `coder.py` so S3 keys, tunnel token, DocuSeal secret key/database URL, Moodle DB/admin credentials, and Coder database/bootstrap-sensitive values are represented as env specs plus placeholders. Preserve Matrix and Headscale placeholder style, but update them to emit structured env specs so the central reconciler owns their values too.
  **Must NOT do**: Do not leave “already placeholder” modules outside the new env-spec validation path.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: broad but mechanical renderer migration.
  - Skills: [`fullstack-dev-skills:python-pro`] - Python test/refactor.
  - Omitted: [`database-optimizer`] - Not query performance work.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 9 | Blocked By: 2

  **References**:
  - Renderers: `src/dokploy_wizard/dokploy/seaweedfs.py`, `cloudflared.py`, `docuseal.py`, `moodle.py`, `coder.py`, `matrix.py`, `headscale.py`.
  - Tests: relevant `tests/unit/test_*pack*.py`, `tests/integration/test_dokploy_bootstrap.py`, `tests/unit/test_compose_secret_leakage.py`.
  - Fixture: `fixtures/moodle-docuseal.env`.

  **Acceptance Criteria**:
  - [ ] Relevant unit/integration tests for SeaweedFS, Cloudflared, DocuSeal, Moodle, Coder, Matrix, and Headscale pass.
  - [ ] Repository-wide leakage test passes for all infra services.
  - [ ] Matrix/Headscale placeholder references have matching structured env specs.

  **QA Scenarios**:
  ```
  Scenario: Tunnel token not in compose
    Tool: Bash
    Steps: Run leakage test with Cloudflare tunnel token fixture.
    Expected: `TUNNEL_TOKEN` raw value absent from compose and present only in cloudflared env spec.
    Evidence: .sisyphus/evidence/task-6-cloudflared-token.txt

  Scenario: Storage credentials scoped
    Tool: Bash
    Steps: Run SeaweedFS render test and leakage test.
    Expected: S3 access/secret keys target only SeaweedFS service env spec and do not appear in compose.
    Evidence: .sisyphus/evidence/task-6-seaweedfs-scope.txt
  ```

  **Commit**: YES | Message: `refactor(infra): move service secrets to dokploy env specs` | Files: listed renderer modules and relevant tests

- [x] 7. Update Pack Catalog/Planner Env Classification

  **What to do**: Extend pack metadata in `src/dokploy_wizard/packs/catalog.py`, resolver/planner outputs in `packs/resolver.py` and `core/planner.py`, or a new colocated metadata module so every env value has a classification: secret/non-secret, required/optional, shared/service-specific, source, owner, target service list, and canonical placeholder name. Use deterministic service-prefixed names like `NEXTCLOUD_POSTGRES_PASSWORD`, `OPENCLAW_LITELLM_VIRTUAL_KEY`, `CLOUDFLARED_TUNNEL_TOKEN` unless a truly shared variable is justified.
  **Must NOT do**: Do not infer least-privilege solely from string prefixes at runtime; use explicit allowlists.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: metadata correctness affects all renderers.
  - Skills: [`fullstack-dev-skills:python-pro`] - Typed metadata contracts.
  - Omitted: [`api-designer`] - Internal model only, not public API.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 9 | Blocked By: 2

  **References**:
  - Catalog: `src/dokploy_wizard/packs/catalog.py`.
  - Resolver: `src/dokploy_wizard/packs/resolver.py`.
  - Planner: `src/dokploy_wizard/core/planner.py`.
  - Tests: `tests/integration/test_selection_flow.py`, `tests/unit/test_compose_secret_leakage.py`.

  **Acceptance Criteria**:
  - [ ] Tests prove no duplicate placeholder names across unrelated services unless explicitly shared.
  - [ ] Tests prove optional empty env values do not produce required placeholders.
  - [ ] Tests prove secret classifications drive redaction and least-privilege targeting.

  **QA Scenarios**:
  ```
  Scenario: Variable collision detection
    Tool: Bash
    Steps: Run env metadata unit test with all packs enabled.
    Expected: No unapproved duplicate secret placeholder names; failures list placeholder names only.
    Evidence: .sisyphus/evidence/task-7-collision-detection.txt

  Scenario: Optional env omission
    Tool: Bash
    Steps: Run metadata test with optional Telegram/R2 values omitted.
    Expected: No required placeholders emitted for omitted optional values.
    Evidence: .sisyphus/evidence/task-7-optional-omission.txt
  ```

  **Commit**: YES | Message: `feat(packs): classify dokploy env ownership` | Files: catalog/resolver/planner or new metadata module and tests

- [x] 8. Harden Redaction, State, and Inspect Output for Env Specs

  **What to do**: Update `src/dokploy_wizard/verification.py`, `src/dokploy_wizard/cli.py`, `src/dokploy_wizard/state/models.py`, `src/dokploy_wizard/state/store.py`, `remote.py`, and `remote_transport.py` so env specs and Dokploy env payloads are always redacted in inspect-state, exceptions, remote collection summaries, and failed API diagnostics. Store only redacted fingerprints/ownership metadata in any new state records; if actual generated values must remain in state, ensure mode `0600` and existing secret-state conventions are reused.
  **Must NOT do**: Do not add raw env payload dumps for debugging.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: cross-cutting security output hardening.
  - Skills: [`fullstack-dev-skills:secure-code-guardian`] - Redaction/security tests.
  - Omitted: [`cso`] - Full audit is final verification.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 9 | Blocked By: 2

  **References**:
  - Redaction: `src/dokploy_wizard/verification.py`.
  - Inspect-state: `src/dokploy_wizard/cli.py`.
  - State: `src/dokploy_wizard/state/models.py`, `src/dokploy_wizard/state/store.py`.
  - Remote: `src/dokploy_wizard/remote.py`, `src/dokploy_wizard/remote_transport.py`.
  - Tests: `tests/unit/test_verification.py`, `tests/test_cli.py`, `tests/test_remote_cli.py`, `tests/test_remote_transport.py`.

  **Acceptance Criteria**:
  - [ ] Redaction tests pass for env spec values, API payload diagnostics, inspect-state JSON, and remote errors.
  - [ ] No new state file stores raw env values except approved generated secret stores with `0600`.
  - [ ] Failure messages include key names/fingerprints only, never raw values.

  **QA Scenarios**:
  ```
  Scenario: Failed API payload redacted
    Tool: Bash
    Steps: Run redaction test simulating Dokploy env API failure with known secret value.
    Expected: Exception/log output contains `<REDACTED>` or fingerprint only; raw value absent.
    Evidence: .sisyphus/evidence/task-8-api-redaction.txt

  Scenario: Inspect-state redacts env specs
    Tool: Bash
    Steps: Run `pytest tests/test_cli.py -q` env/inspect cases.
    Expected: Inspect output reports env key presence/ownership without values.
    Evidence: .sisyphus/evidence/task-8-inspect-redaction.txt
  ```

  **Commit**: YES | Message: `fix(security): redact dokploy env specs everywhere` | Files: listed redaction/state/remote modules and tests

- [x] 9. Preserve No-op, Rerun, and Modify Safety with Env Metadata

  **What to do**: Update `src/dokploy_wizard/dokploy/compose_noop.py` and lifecycle integration so unchanged safe compose plus unchanged env spec metadata skips deploy, changed env names/scopes trigger update, and secret value changes reconcile env without writing raw values into hash state/logs. For existing installs, fail safe and preserve services; do not build full in-place migration as a primary deliverable. Fresh installs must always create env before deploy.
  **Must NOT do**: Do not hash raw secret values. Do not mark a service no-op if required env specs are missing in Dokploy payload planning.

  **Recommended Agent Profile**:
  - Category: `deep` - Reason: lifecycle correctness and idempotency.
  - Skills: [`fullstack-dev-skills:python-pro`] - Python lifecycle tests.
  - Omitted: [`database-optimizer`] - Not DB performance.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: 10 | Blocked By: 3-8

  **References**:
  - No-op: `src/dokploy_wizard/dokploy/compose_noop.py`.
  - Lifecycle: `src/dokploy_wizard/lifecycle/engine.py`.
  - Tests: `tests/unit/test_compose_noop.py`, `tests/integration/test_dokploy_bootstrap.py`, `tests/e2e/test_rerun_modify_resume.py`.

  **Acceptance Criteria**:
  - [ ] `pytest tests/unit/test_compose_noop.py tests/integration/test_dokploy_bootstrap.py tests/e2e/test_rerun_modify_resume.py -q` passes.
  - [ ] No-op state stores safe compose hash plus redacted env metadata/fingerprint only.
  - [ ] Fresh install ordering proves env reconciliation occurs before first deploy.

  **QA Scenarios**:
  ```
  Scenario: Unchanged rerun skips deploy
    Tool: Bash
    Steps: Run compose no-op tests for unchanged safe compose and env metadata.
    Expected: No update/deploy recorded; no raw env values in hash state.
    Evidence: .sisyphus/evidence/task-9-noop-unchanged.txt

  Scenario: Env spec change triggers update
    Tool: Bash
    Steps: Run test changing env target service/name while keeping compose text stable.
    Expected: Env reconciliation/update recorded; deploy behavior follows existing policy; raw values absent from state/logs.
    Evidence: .sisyphus/evidence/task-9-env-spec-change.txt
  ```

  **Commit**: YES | Message: `fix(dokploy): preserve noop with env metadata` | Files: `compose_noop.py`, lifecycle/backends, tests

- [ ] 10. Run Full Validation and Fresh-VPS Proof Artifact Checks

  **What to do**: Run the full local suite, type/lint checks, and fresh-VPS proof. Add or update an agent-executable artifact checker that scans collected remote state/logs/compose artifacts for known `.install.env` secret values and env-spec raw values, while allowing redacted fingerprints/key names. Store proof output under `.sisyphus/evidence/dokploy-env-secrets/`.
  **Must NOT do**: Do not paste raw host password or secret values into evidence.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: end-to-end validation and evidence collection.
  - Skills: [`fullstack-dev-skills:test-master`] - Suite/proof execution discipline.
  - Omitted: [`qa`] - This is CLI/VPS proof, not browser app QA.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: Final Verification | Blocked By: 9

  **References**:
  - Remote helper: `bin/dokploy-wizard-remote`.
  - Verification runner: `src/dokploy_wizard/service_verification_runner.py`.
  - Remote tests: `tests/test_remote_cli.py`, `tests/test_remote_transport.py`.
  - README/AGENTS proof command documentation.

  **Acceptance Criteria**:
  - [ ] `pytest -q && ruff check . && mypy .` passes.
  - [ ] Fresh VPS proof command succeeds on first install.
  - [ ] Artifact checker proves collected compose/log/state artifacts contain no raw `.install.env` secret values.
  - [ ] Evidence files are saved under `.sisyphus/evidence/dokploy-env-secrets/` with secrets redacted.

  **QA Scenarios**:
  ```
  Scenario: Full local regression
    Tool: Bash
    Steps: Run `pytest -q && ruff check . && mypy .`.
    Expected: Exit code 0 for all commands.
    Evidence: .sisyphus/evidence/task-10-local-suite.txt

  Scenario: Fresh VPS proof
    Tool: Bash
    Steps: Run `./bin/dokploy-wizard-remote proof --host <fresh-test-host> --password <REDACTED> --env-file ./.install.env`.
    Expected: Install, service verification, inspect-state, and artifact collection succeed; artifact scan finds zero raw secrets.
    Evidence: .sisyphus/evidence/task-10-fresh-vps-proof.txt
  ```

  **Commit**: YES | Message: `test(security): verify fresh vps dokploy env scoping` | Files: artifact checker/tests/evidence references as appropriate

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [ ] F1. Plan Compliance Audit — oracle
- [ ] F2. Code Quality Review — unspecified-high
- [ ] F3. Real Manual QA — unspecified-high (+ fresh VPS proof/artifact scan; Playwright only if verifying browser surfaces)
- [ ] F4. Scope Fidelity Check — deep

## Commit Strategy
- Commit after each task using the specified message.
- Do not squash during execution unless the user explicitly requests it.
- Never commit `.install.env`, raw remote logs with secrets, or unredacted proof artifacts.

## Success Criteria
- All wizard-managed Dokploy services use safe compose YAML plus Dokploy env reconciliation.
- Fresh-VPS proof installs successfully from scratch with env present before deploy.
- No raw secret values appear in compose YAML, logs, inspect-state, state JSON, no-op hash artifacts, API diagnostics, or collected remote proof artifacts.
- Least-privilege env targeting is enforced by tests: service receives only explicitly allowed variables.
