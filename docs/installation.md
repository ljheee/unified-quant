# Installation and Initial Bootstrap

The installer copies the reusable framework into an existing project and can register project-level `SessionStart` hooks. It is ownership-aware, not a legacy migration tool.

## Safety precondition: one writer

Before installation, pause every other writer of the target tree and configuration: other Claude/Codex sessions, editors and formatters, sync tools, generators, and installers. Keep them paused through reads, proposals, publication, verification, and any recovery. The installer’s advisory lock coordinates cooperating installers only; it cannot stop a non-cooperating writer. A clean final check is not a compare-and-swap guarantee.

If this single-writer condition cannot be maintained, do not authorize replacement of existing files. Preserve the originals and resolve the change manually. If a transaction journal or recovery entry is present, inspect it and complete manual recovery before another install.

Unsupported filesystems, atomic exchange or rollback primitives, metadata APIs, and ambiguous recovery residue are explicit diagnostics and safe refusals. They are never reasons to fall back to an unchecked overwrite. The existing single-writer limitation remains in force.

The unresolved stronger-model diagnostic remains `tests/test-installer-ctime-gate.py`: a non-cooperating same-UID writer can change the file after the final check. It is not a supported-model pass and is not fixed by this ownership refactor. CI retains its separate explicit `run_unsupported_diagnostics` workflow-dispatch path and its real failing assertion, without `expectedFailure` or `continue-on-error`.

## Usage

From a reviewed, fixed-version checkout:

```bash
./install.sh
# or, from another directory:
/path/to/self-evolution-flywheel/install.sh --target /path/to/project
```

The target must already exist. Hooks require the target to be the Git work-tree root; use `--no-hooks` for file installation without hook settings. The root `install.sh` in the source checkout is the only source entry. The target project’s own `install.sh` is never installed, copied, opened, or inspected, even when it is missing. The installed project-local entry is `./.agent-maintenance/flywheel-install.sh`; it is a dedicated entry for that project, not an independent source or repair mechanism.

A GitHub **Use this template** copy contains its seed documentation, prompts, and other repository files because Git created the copy; that does not mean the current installer seeds `README.md`. The installer’s target file list is defined by the implementation, not by this README.

A pipeline invocation is possible only when the reviewed pipeline support is present in the chosen branch:

```bash
curl -fsSL https://raw.githubusercontent.com/jack4leo/self-evolution-flywheel/main/install.sh | bash
```

This immediately executes a remote script; inspect and pin a reviewed checkout when provenance matters. Use `--bootstrap codex` or `--bootstrap claude` only to explicitly start initialization. Without that option, installation does not start an agent or populate project documentation.

## Ownership and update rules

| Actual destination / content | Owner | First install | Repeat install / template update |
| --- | --- | --- | --- |
| `.agent-maintenance/flywheel-install.sh` | Framework (source: template root `install.sh`) | Create dedicated entry | Update only from known manifest/source bytes; reject custom/unknown content |
| `.agent-maintenance/{installer-transaction.py,framework-ownership.py,session-start.sh}` | Framework | Create | Update with selected template version, same checks |
| `scripts/{freeze-review-range.py,write-frozen-review.py,validate_lesson_promotions.py,watermark.py}` | Framework | Create | Update with selected template version, same checks |
| `.github/workflows/lesson-promotion-gate.yml` | Framework | Create | Update with selected template version, same checks |
| `.agent-maintenance/framework-version.json` | Framework version metadata | Create after verified publications | Record verified revision, content digest and bounded hashes |
| `AGENTS.md` managed routing block | Framework region; all outside bytes belong to project | Create source file if absent | Replace only the uniquely marked region; invalid markers refuse |
| `docs/project-profile.md` framework block | Framework region; facts and all outside bytes belong to project | Seed marked profile if absent | Replace only known framework block; never migrate facts |
| `.claude/settings.json` | Project JSON; exact Flywheel hook is framework-owned | Create hook config if absent | Add missing canonical hook; ambiguity requires diff confirmation; retain all user fields/hooks |
| `.codex/hooks.json` | Project JSON; exact Flywheel hook is framework-owned | Create hook config if absent | Accept exact hook, otherwise retain and request manual merge |
| `LESSONS.md`, `CLAUDE.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `prompts/**`, installed `docs/**` except managed profile region | Project (seed-only) | Create absent files | Preserve existing bytes, including standards, subsystem facts and review policy |
| Target root `install.sh`, `README.md`, `docs/source-and-extraction.md`, other project files | Project | Not installed or changed | Not installed or changed |


The exact framework-owned set is fixed by `.agent-maintenance/framework-ownership.py`: `.agent-maintenance/flywheel-install.sh`, `.agent-maintenance/installer-transaction.py`, `.agent-maintenance/framework-ownership.py`, `.agent-maintenance/session-start.sh`, `.github/workflows/lesson-promotion-gate.yml`, `scripts/freeze-review-range.py`, `scripts/write-frozen-review.py`, `scripts/validate_lesson_promotions.py`, `scripts/watermark.py`, and `.agent-maintenance/framework-version.json`. It is not an arbitrary `scripts/*.py` wildcard.

`framework-version.json` records the source Git revision and content hashes. A framework file may be updated when its current content matches the source or the prior manifest hash, regardless of whether the source Git revision is numerically or chronologically “newer.” The source revision identifies the checkout; the content digest identifies its actual (possibly modified) contents. Unknown conflicts are preserved and reported; there is no legacy migration.

`prompts/`, `CLAUDE.md`, `.github/PULL_REQUEST_TEMPLATE.md`, and installed `docs/` files other than the marked profile framework region are seed-only content. `README.md` and `docs/source-and-extraction.md` are not installed. `LESSONS.md` is project-maintained state. They are not silently replaced. The target project’s `install.sh` is never a framework file.

### Managed text

- **`AGENTS.md`:** a missing file is created. A valid existing managed routing block is updated in place automatically, with no prompt; text outside the block is preserved. If an existing file has neither routing marker, installation fails closed and does **not** prepend a block. Missing, duplicated, out-of-order, or otherwise anomalous markers are preserved and reported rather than guessed around. A refusal leaves the file unchanged.
- **`docs/project-profile.md`:** only the framework section between its exact framework markers is managed. The project-facts section and all other bytes remain unchanged. Missing, duplicated, or malformed profile markers fail closed; they do not authorize replacing or migrating existing facts.
- **Other project files:** existing content is preserved unless the implementation explicitly recognizes it as owned and its prior manifest hash authorizes the update. Unknown or seed-only conflicts are not overwritten.

### Provider hooks

The canonical commands are:

```text
bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex
bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude
```

Missing settings files are created, and an already registered exact hook is accepted without duplication. A valid existing Codex settings file that lacks the hook is preserved for manual merging.

Claude registration is additive: a missing canonical hook is added automatically. If an existing command contains `session-start.sh` or `flywheel`, ownership is ambiguous; the installer shows the exact diff and applies it only after an interactive exact `y`/`Y` response. Any other response, EOF, no terminal, or decline preserves the original. Custom hooks and unrelated JSON fields are never deleted; the merge preserves the original JSON text and rejects duplicate keys. Skipping Claude does not block Codex or conflict-free framework files.

## Recovery and publication limits

The installer records writes in a transaction journal and retains displaced originals as visible recovery backups when needed. It may also retain rollback entries or incomplete-recovery state. These artifacts are not an automatic atomic rollback guarantee: after a failure, a moved directory, open writable descriptor, concurrent writer, power loss, or unsupported operation, inspect the journal and original/recovery entries and restore manually. Do not delete recovery material or retry around uncertainty. Independent files that completed before a later failure may remain installed.

## Startup hooks and bootstrap

The hook script does not start a model, execute the bootstrap prompt, inspect source code, write documentation, or use the network. It checks `.agent-maintenance/bootstrap-complete` and required non-empty regular project documents. A valid marker contains a standalone `Reviewed HEAD: <40- or 64-character lowercase SHA>` line; otherwise the hook emits its diagnostic and reminder behavior. A missing or symlinked `prompts/bootstrap-project.md` remains an explicit nonzero stderr diagnostic; it is not hidden or treated as successful completion. When bootstrap is pending and the prompt exists, the hook prints a reminder only and does not interrupt the user’s task.

Start initialization explicitly:

```bash
./.agent-maintenance/flywheel-install.sh --bootstrap codex
# or
./.agent-maintenance/flywheel-install.sh --bootstrap claude
```

The CLI must be installed and available in `PATH`. Trust prompts and project-setting permissions remain controlled by Codex or Claude Code.

## Verification

Run the repository’s installer suite with:

```bash
bash tests/test-install.sh
python3 tests/test-framework-ownership.py -v
```

The tests cover ownership, marker handling, provider preflight and additive hooks, metadata and recovery behavior, single-writer races, journal failures, marker/document diagnostics, and bootstrap lock release. The official hook references are [Codex hooks](https://developers.openai.com/codex/hooks) and [Claude Code hooks](https://code.claude.com/docs/en/hooks).
