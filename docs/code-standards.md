# Code standards

This document defines the minimum review standards applicable to all projects, as well as project-specific conventions continuously maintained by the agent based on **current project evidence**. The two kinds of rules must remain in separate sections; do not treat template placeholders, another repository's habits, or unverified assumptions as this project's rules.

## A. General baseline (does not change with the project stack)

### Change correctness

- Understand existing behavior and the single source of truth before changing anything; trace the complete entry points, state/data flow, error paths, and observable side effects, and check analogous entry points and compatibility paths.
- Keep changes to the minimum necessary scope of the request. Avoid opportunistic refactoring, unrelated formatting, and unrequested product behavior changes.
- When fixing a defect, add a regression test that expresses externally observable behavior; where feasible, confirm that the test fails against the old implementation or when the fix is removed. If an automated test cannot be established, record the specific reason and alternative validation; do not describe it as covered.
- Validate the contract, including normal inputs, boundary/exception inputs, state persistence or restart, compatibility with older configuration, and failure handling (as relevant to the change).

### Data, security, and compatibility

- Do not put secrets, user data, or real credentials in test fixtures, logs, screenshots, commits, or reports; use isolated environments for external services and destructive operations.
- Do not mistake unreadable configuration/data for empty configuration and overwrite user data; for stateful writes, prefer preserving existing atomicity, rollback, and recovery conventions.
- Make clear whether the change affects interfaces, serialization formats, migrations, permissions, authentication, network boundaries, observability fields, dependencies, or release artifacts; cover compatible behavior and failure modes.
- Retries, degradation, or fallback must have explicit trigger conditions, idempotency/safety rationale, ordering, and termination conditions; do not silently change the semantics of user input.
- Logs and traces must be sufficient to explain decisions and errors, while following minimization principles and avoiding unnecessary content or sensitive fields.

### Testing and review

- Use the project's actual formatter, static checks, targeted tests, and necessary full-suite tests; report only commands and results that were actually run.
- Test inputs should preserve the structure of real requests, configuration, or disk formats as far as possible, and cover missing and boundary values relevant to the change; fixtures must remove secrets and real personal data.
- For important defect fixes, where feasible revert the fix, inject a fault, or break the protected behavior to confirm that the regression test actually fails; explain limitations preventing this check, and do not treat the mere existence of a test as proof of effective coverage.
- Clean up child processes, asynchronous tasks, temporary directories, ports, and other resources started by tests; check that failure and cancellation paths also terminate or release resources.
- Select platform, client, and alternative-implementation validation according to the change's impact and the project's support scope; explicitly list untested combinations, and do not require unrelated projects to run a nonexistent matrix.
- Check the implementation, tests, documentation, migrations/examples, and adjacent paths together; preserve validation of important compatibility constraints.
- The final report must state clearly: validated items, unvalidated items and reasons, affected subsystems, and the reviewed commit SHA. Checks not run must not be described as passing.
- If the code HEAD changes during validation, review and validate the final HEAD again; before merging, confirm that required checks target the version under review.

## B. Project-specific standards (continuously maintained by the agent)

> Bootstrap must complete this section from the project's source code, tool configuration, CI, existing rules, and real retrospectives; mark unknowns as `UNKNOWN` and include questions requiring confirmation. Do not copy template examples.

### Technology and repository conventions

- Language, framework, and directory/module conventions: Python 3.11+ with `from __future__ import annotations` and modern type syntax; `src/` layout with the package `uq` (`pyproject.toml`), one responsibility per module (`contracts/`, `adapters/`, `routing/`, `quality/`, `store/`, `factors/`, `models/`, `market/`, `pipeline/`, `portfolio/`, `backtest/`, `risk/`, `execution/`, `research_chain/`), with production publishers living in `scripts/` rather than inside `src/uq/`. Canonical identity/serialization rules live once in `src/uq/contracts/` and are shared by writers and readers; duplicate a rule rather than a constant only after checking both sides cannot drift. Fail-closed policy decisions are expressed as `uq.errors.ContractError` subclasses (`CapabilityGapError`, `PartialDataNotRequestedError`, `IndependentCalendarError`) so the CLI can map them to exit codes — use the shared classes in `src/uq/errors.py`, never a locally redefined `ContractError` (that class-identity defect is recorded in `cr.md` §8.16 B7).
- Formatting, linting, type checking, and build commands: `UNKNOWN` — none configured. No formatter, linter, type checker or pre-commit hook exists in this repository, so do not claim a style gate passes. Conventions observed in the source: four-space indent, ~100-column lines, docstrings on public classes explaining *why* the constraint exists, `Path` for filesystem arguments, frozen dataclasses for contracts, and no new dependency without an owner decision.
- Dependency, code-generation, migration, and release conventions: dependencies are pinned through `uv.lock` and consumed only via `scripts/run_gate.sh` (`uv sync --locked --extra dev --extra real`, plus `--extra qlib` for the Qlib cell); the fallback path uses `scripts/export_locked_requirements.py`. No code generation exists. Schema/contract versioning is the migration mechanism: a semantic or unit change publishes a new version alongside the old one, and readers dispatch on the version in the artifact (see `src/uq/contracts/adjustment_snapshot.py:25` for the worked precedent and `:48` for the version dispatch, and `src/uq/contracts/canonical_v2.py:278` `CanonicalMigrationLedger`). Releases are manual and evidence-bound: `evidence/*/release/**` record the release head, CI run id and covered cells, and `scripts/verify_research_evidence.py` / `tests/test_research_chain_release_evidence.py` verify those ledgers mechanically.

### Acceptance and validation requirements

- Targeted and full test commands (including working directory and tags/arguments): run from the repository root. Full suite: `.venv/bin/python -m pytest` (configured `addopts = "-q"`, `testpaths = ["tests"]`); with the Qlib extra: `.venv-qlib/bin/python -m pytest`. Targeted: `.venv/bin/python -m pytest tests/<file>.py -q`. The certification gate is `scripts/run_gate.sh` (set `UQ_GATE_EXTRAS=qlib` for the Qlib cell), which writes `.gate/gate-report.json` and must be the command reported for any release claim. Qlib-gated tests use `pytest.importorskip("qlib")`-style skips, so a skip count of ~25 in `.venv` is expected and must be attributed to the missing extra rather than described as a failure.
- Required CI workflows and passing conditions: `.github/workflows/ci.yml` — `test` job passes on macOS/Ubuntu x Python 3.11/3.12/3.13 with real-source imports verified; `qlib-runtime` passes on macOS/Ubuntu x 3.11/3.12 with `UQ_GATE_EXTRAS=qlib`; `f4-certification` requires all ten declared cells present with `test_result == passed`, matching `git_commit` and matching declared Python version. `.github/workflows/lesson-promotion-gate.yml` runs `python3 scripts/validate_lesson_promotions.py` and is read-only. A change that alters implementation or `uv.lock` invalidates the previous certification and requires a fresh local + remote gate before any release claim.
- Integration/real-environment tests, isolation method, and untestable scope: no live-vendor test exists or may be added silently. Vendor behaviour is asserted with injected openers and fake clients (`tests/test_eastmoney_calendar.py`, `tests/test_real_chain.py`, `tests/test_mootdx_source.py`); all state lives in `tmp_path`. Untestable scope: live TDX/Tushare/EastMoney reachability, real ingest, real publishes under `scripts/publish_*.py` and `scripts/build_*.py`, AkShare-dependent paths (not installed in either local virtualenv), and any broker/live execution behaviour, which is prohibited by design.
- Project-specific normal, boundary, failure, rollback, and compatibility test requirements: for every behaviour change cover (a) the normal path, (b) the boundary that motivated the change (empty frame, single row, `high == low`, warm-up lead-in, window edge), (c) each declared failure branch with its typed error and CLI exit code, and (d) compatibility — older schema/manifest versions must stay readable. Rollback/discovery coverage means proving the gate *rejects*: tampered manifest, wrong generation binding, missing quality decision, symlink inside a store root, out-of-window session, non-finite feature, and a store root spelled through a symlink (the macOS `/tmp`/`/private` case, `tests/test_external_review_20261014.py`).

### Project-specific risks and stable rules

- Sensitive paths, credentials, personal information, and high-risk operations: `.env` and Tushare tokens (never commit, never log, CLI does not read `.env`); external `--data-root` directories; Ed25519 review keys — repository trust-anchor JSON files hold public keys only, and `src/uq/runtime.py:34` `require_production_review_key` forbids the repository test-mode anchors under `UQ_RUNTIME_MODE=production`, so a test-signed review can never authorize a production run. Generated market data, Qlib `.bin` files, credentials and data caches must never be committed (`AGENTS.md`, `.gitignore`). High-risk operations: touching `.github/workflows/**`, `uv.lock`, `src/uq/runtime.py`, `src/uq/contracts/paths.py`, any immutable publisher, `config/schemas/**`, or the release ledgers; and running any script that writes real artifacts.
- Confirmed design/compatibility decisions and their sources: (1) source adapters are never the factor API (`AGENTS.md`, `README.md`, `specs/architecture.md:2`); (2) canonical schema semantic or unit changes require a new version, and the version dispatch must keep older artifacts readable (`AGENTS.md`, `specs/architecture.md:3`, worked precedent in `src/uq/contracts/adjustment_snapshot.py:25`); (3) no silent partial coverage — partial requires an explicit opt-in and a `partial` marker (`src/uq/routing/router.py:55`); (4) publishers may never self-approve a quality decision (`evidence/research-chain/release/release-record.json`); (5) `contract_version` 3/4 research requests stay non-executable until the paper runtime lands (`src/uq/research_chain/runner.py:334`); (6) paper execution is deterministic replay, broker/live paths prohibited (`specs/architecture.md` header); (7) the environmental-certification claim is cell-scoped, never cross-platform byte equality (`specs/layers/contracts/f4-environment-certification-v1.md`). Product direction, security exceptions, contributor policy and merge/release authority are maintainer decisions and must be escalated, not assumed.
- Stable rules promoted from Lessons are listed in the block below. Give each rule a unique `PS-0001` ID and cite one or more promoted `L-0001` lesson IDs. The cited lessons must collectively contain at least three distinct, structured review-evidence dates as defined in [`LESSONS.md`](../LESSONS.md).

#### Stable promoted rules

The following bounded block is the authoritative location for project-specific stable promoted rules. Do not count one-off temporary safeguards as stable promotions.

<!-- PROMOTED_RULES:START -->
##### PS-0001 — Governed-store symlink policy is "in store only": nothing above the root is checked, every component from the root down must not be a link, the resolved path must land inside the resolved root, and a test must spell the store root through a real symlink
- **Scope:** any governed store, and any change to store path handling (`src/uq/contracts/paths.py` and all its callers)
- **Evidence lessons:** `L-0001`

##### PS-0002 — A change that touches a source file pinned by an evidence index re-pins that digest in the same change, verifies by re-reading the index, and investigates a freshness red against the stash state before attributing it elsewhere
- **Scope:** any change to a file pinned by an `evidence-index.json` under `evidence/**`
- **Evidence lessons:** `L-0002`
<!-- PROMOTED_RULES:END -->

If the block is empty, no project-specific stable rule is currently promoted.

Each rule in the block above uses this format:

```markdown
##### PS-0001 — <one executable rule>
- **Scope:** <where the rule applies>
- **Evidence lessons:** `L-0001`, `L-0002`
```

## Maintaining this standard

- The agent may update section B using evidence from `LESSONS.md` and the distinct-date promotion rule; each new convention must state its scope and include an evidence link. Stable promoted rules must use the IDs and cross-references above.
- The read-only Lesson-promotion CI gate validates the persisted record structure and distinct-date threshold. It does not calculate review ranges, create archives, or read or update the `reviewed-through` watermark; humans remain responsible for assessing the cited evidence's authenticity and meaning.
- Section A is the general baseline. If removal or relaxation is needed, first propose a maintainer decision with a risk explanation; do not quietly expand a one-off local exception into a default.
- Whenever CI, test commands, the toolchain, architecture, or an approved policy changes, check whether this section and `docs/project-profile.md` remain accurate.
