<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->
# Agent Work Entry Point

This is a routing table, not a project encyclopedia. Keep rules and facts in their single authoritative documents, and keep this file brief when editing documentation.

## Before Starting a Task

1. Read [`docs/project-profile.md`](docs/project-profile.md) to learn the project's confirmed technology stack, operating method, verification commands, and permission boundaries; do not treat `UNKNOWN` as known on your own.
2. Read [`LESSONS.md`](LESSONS.md) to avoid repeating recently encountered problems.
3. When modifying or reviewing code, follow [`docs/code-standards.md`](docs/code-standards.md). Project-specific sections must be maintained from evidence in the repository's source code, tests, CI, and retrospectives.
4. When affecting a documented subsystem, first read [`docs/subsystems/README.md`](docs/subsystems/README.md) and the corresponding subsystem page; if actual behavior, interfaces, state, compatibility, or failure modes change, update that page in the same change.
5. When reviewing community/external contributions, first read [`docs/contributors.md`](docs/contributors.md), especially the risk checks before running untrusted code and cases requiring a maintainer decision.

## Execution and Delivery

- Confirm the request and observable goal first, then trace the complete call path and affected peer entry points; keep the change focused.
- Add regression verification that expresses user behavior for bug fixes; where feasible, demonstrate that the test fails on the old/broken implementation. Do not describe compilation failures or skipped tests as verified behavior.
- Verify with the real commands in the project profile. Report exactly what was run, the results, the commit/worktree version, and any checks not run with the reasons.
- Do not claim to have run tests against real services, platforms, or devices; when verification is unavailable, state the limitation and avoid promising unverified behavior as usable.
- At completion, check related documentation, adjacent implementations, configuration migrations, and tests; PR/commit descriptions must cite the exact HEAD reviewed.

## Ongoing Maintenance

- Use [`prompts/daily-review.md`](prompts/daily-review.md) to review daily commits; record evidence-backed rework, incorrect merges, or insufficient verification in `LESSONS.md`. Good verification may also be recorded under `Done right`.
- The Agent maintains the project-specific sections of Lessons and `docs/code-standards.md`: follow the date/evidence format and promotion threshold in `LESSONS.md`, and do not turn one-off events or guesses into hard rules.
- Product direction, explicit maintainer decisions, security exceptions, contributor bans, merge/release permissions, and other matters beyond the authorized scope must be proposed and stopped at the decision point; do not independently reverse an explicit decision.
- A missing, invalid, or stale `.agent-maintenance/bootstrap-complete` marker is only a reminder: do not preempt the user's requested task or fill bootstrap documentation unless the user explicitly asks for bootstrap. An explicit request for template integration or project initialization authorizes bootstrap within that scope; the literal word "bootstrap" is not required. The project owner can start it with `./.agent-maintenance/flywheel-install.sh --bootstrap codex` or `./.agent-maintenance/flywheel-install.sh --bootstrap claude`; then follow [`prompts/bootstrap-project.md`](prompts/bootstrap-project.md), use only this repository's evidence, and write the marker only after completing the documentation review and verification.
<!-- END SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->

# Repository Instructions

- Python code targets 3.11+ and uses modern type syntax.
- Keep source adapters isolated from canonical factor APIs.
- Never commit generated market data, Qlib `.bin` files, credentials, or data caches.
- Canonical schema changes that alter semantics or units require a new version.
- Run `python -m pytest` before handing off changes.
- Full-suite commands, the certification gate (`scripts/run_gate.sh`), CI cells and permission boundaries live in [`docs/project-profile.md`](docs/project-profile.md); use `.venv-qlib/bin/python -m pytest` when Qlib behaviour is in scope, and never describe a Qlib-gated skip as a failure.
- Reviewable code lives under `src/uq/` with production publishers in `scripts/`; the authoritative behaviour of each area is the matching page in [`docs/subsystems/`](docs/subsystems/README.md). Do not infer a boundary from a folder name.
- Findings from review rounds belong in `cr.md` (tracked) with a section per round; external review reports stay untracked unless the owner asks otherwise. Follow [`docs/contributors.md`](docs/contributors.md) for external contributions.
- This project has no formatter, linter or type checker; adding one is a maintainer decision (do not add one unilaterally), and do not claim a style gate passes (see [`docs/code-standards.md`](docs/code-standards.md)).


<claude-mem-context>
# Memory Context

# [unified-quant] recent context, 2026-10-18 12:49am GMT+8

No previous sessions found.
</claude-mem-context>
