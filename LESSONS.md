<!-- reviewed-through: UNKNOWN (bootstrap the project before using this watermark) -->
# Project Lessons and Retrospectives (Lessons)

This is the target project's living incident and learning log, not a collection of generic examples. Except for the maintenance protocol defined in this file, every entry must come from commits, issues/PRs, CI output, reproduction records, or verifiable maintainer feedback inspected for this project; do not copy specific technical rules from templates or other projects.

## Maintenance protocol

- Daily review scope: run `scripts/freeze-review-range.py` and use its canonical manifest as the sole range authority; do not calculate the range from this marker or a later HEAD. If the marker is `UNKNOWN`, stop and ask a maintainer to choose the first target-branch commit to include; because the boundary is exclusive, use that commit's target-branch parent (the target-branch/first parent for a merge). Do not infer or auto-initialize this starting boundary; after explicit establishment, only the normal completed-review writer advances it to `through`. Review every commit in the manifest, but skip only the contents of each commit/parent/path recorded in `excluded_paths`; exclusions do not remove commits or their other paths. Record excluded paths and reasons in the archive. The `generated-data-paths` rule accepts only explicit literal paths and defaults empty; do not guess a target project's generated-data locations.
- Focus on errors in development, code review (CR), merge/release, and verification: **fixed and then reworked**, **merged/released when they should not have been**, and **insufficient validation or an untrusted validation baseline**. Do not review contents of generated-data snapshots excluded by the frozen manifest as Lessons, regardless of whether they came from training, an intermediate stage, ETL, a script, runtime behavior, or another generation process. A workflow or generated-data anomaly may be recorded as an automation-health/verification issue only with independent workflow, CI, validator, or similar evidence. Also record `Done right` to preserve effective practices.
- Write rules as short, executable constraints for next time; include the incident cause, impact, fix/follow-up action, and evidence links. When evidence is insufficient, phrase it as an open question, not as a confirmed causal claim.
- Count recurrence by **distinct completed review dates** on which the lesson appeared. Multiple commits or repeated events on the same day count as one day; do not inflate the count through repeated edits or by splitting a review.
- By default, only after a generalized stable rule has appeared independently in reviews on **at least 3 different dates** may it be promoted to the project-specific section of [`docs/code-standards.md`](docs/code-standards.md). During promotion, move the complete lesson record to promotion history, preserve its ID, dates, and citations, and note the promoted rule ID; do not lose the source.
- For a serious one-time security, data-loss, or release incident, an agent may immediately propose a temporary safeguard. Mark the proposal `Temporary rule / pending maintainer confirmation`; this does not count as a three-day promotion or authorize an unapproved product policy.
- After updating a rule, check AGENTS routing, relevant subsystem pages, tests, and other entry points for conflicts; keep one authoritative definition and do not duplicate it by copy-paste.
- Even when nothing is found that day, archive the frozen scope, excluded-path summary, sources checked, and “no new evidence-backed lesson”. Do not directly edit this file or advance the marker during a review; pass archive and Lessons content to `scripts/write-frozen-review.py`, which verifies the updates and advances `reviewed-through` to the manifest's `through` as its final durable write. A first-run `UNKNOWN` marker is not writer input: obtain the maintainer's starting-commit decision separately and keep this marker unchanged until an explicitly approved boundary is established.
- The Lesson-promotion CI check is read-only and checks only the persisted `LESSONS.md` and `docs/code-standards.md` records. It does not calculate or select a review range, inspect commits for a retrospective, create review archives, or read or update `reviewed-through`.

When recording a specific event, evidence may support checking whether it can be reproduced using the reporter's surface, scenario, and inputs; whether test fixtures preserve the shape of the real request or disk data; whether processes, asynchronous tasks, and temporary resources started by tests are cleaned up on both success and failure paths; and whether existing baseline failures were investigated and the conclusion recorded. These are review leads, not assumptions that this project already has these problems or must adopt a particular response; in particular, do not generalize one project's policy for handling red tests into a rule for all projects.

## Record identity and evidence schema

- Give every lesson a stable ID of the form `L-0001` (the numeric suffix has at least four digits). Assign the next unused ID; never change it when wording, dates, or status change, and never reuse an ID. IDs must be unique across the active-lessons and promotion-history blocks.
- Keep active records between `ACTIVE_LESSONS:START` and `ACTIVE_LESSONS:END`, and promoted records between `PROMOTED_LESSONS:START` and `PROMOTED_LESSONS:END`. Each record heading starts `### L-0001 —` followed by the executable rule.
- Under **Review evidence**, each row begins with a backtick-delimited ISO date, followed by an em dash and a Markdown link to specific inspected project evidence. The date is the date of the completed review that observed the lesson, not a commit timestamp, event date, or promotion date. Each row must cite concrete evidence inspected for that date. Multiple rows or citations from the same review date count once.
- A promoted standard uses a stable ID of the form `PS-0001`. Its standards entry must cite one or more lesson IDs, and the union of those lessons' structured review-evidence rows must contain at least three different valid dates. The corresponding promoted lesson record names that `PS-` ID.
- A promotion date is traceability metadata, not evidence. Legacy `Seen N×` text or a generic Evidence field without structured date-specific citations does not count toward the threshold. Migrate only dates and citations supported by repository evidence; do not infer review dates from commit dates, repeated mentions, or the date of this migration. If the evidence cannot be verified, leave the rule unpromoted and state the migration gap.
- CI checks ID syntax/uniqueness, cross-references, date syntax/distinctness, and citation shape. It cannot determine whether a cited artifact is genuine, whether the reviewer actually inspected it, whether the observations are semantically the same lesson, or whether the evidence justifies the rule; those remain human-review responsibilities.

## Active lessons

<!-- ACTIVE_LESSONS:START -->
### L-0001 — Store roots are "in store only" for symlink policy, and prove it with a symlinked root in the test
- **Why:** The governed-store symlink guard walked every lexical *ancestor* of the path it was given, so a legitimate store root spelled through macOS `/tmp` → `/private/tmp` (or any symlinked project directory) was refused outright, while a link placed *inside* a store root could still be reached depending on how the path was spelled. The suite never caught either direction because pytest hands tests an already-resolved `tmp_path`, so no test ever spelled a root through a link. Three sibling call sites kept the ancestor-walking variant after the shared guard was introduced, and the guard's own "resolved path" fallback was itself an accept-on-link hole.
- **Review evidence:**
  - `2026-09-15` — [cr.md:135, finding 10 verdict: the ancestor symlink walk makes every macOS tmp-root store read fail](cr.md#L135)
  - `2026-10-13` — [cr.md:555 B9, single-source `reject_symlinks_within` scoped at and below the root](cr.md#L555)
  - `2026-10-14` — [cr.md:679 B9-A/B/C/D round (evidence lines through :698), three residual call sites and the resolved-path hole, with the consistent-spelling rule](cr.md#L679) and, in the same round, [tests/test_external_review_20261014.py, every test builds its root through a real symlink the way macOS spells `/tmp`](tests/test_external_review_20261014.py)
- **Fix or follow-up:** `src/uq/contracts/paths.py:32` `reject_symlinks_within` is the single guard: nothing above the root is checked, every component from the root down must not be a link, and the resolved path must still land inside the resolved root. Callers must spell `path` and `root` consistently (both unresolved or both resolved), because a resolved path has already dropped the very link the guard exists to find. When adding a store or changing its path handling, reuse this guard and add a test whose root is a real symlink — a resolved `tmp_path` cannot detect a regression here.
- **Done right:** The rule and its rationale live in the `src/uq/contracts/paths.py` module and function docstrings, and the negative locks cover link-inside-store, out-of-root and `..` escape, so the fix is documented where the next caller will read it.
- **Status:** active
- **Distinct review dates observed:** 3 (2026-09-15, 2026-10-13, 2026-10-14) — meets the promotion threshold, but promotion is a separate write through the frozen-review writer with a maintainer-authorized range, so it is not performed here

### L-0002 — Re-pin every evidence-index digest a source change touches, and re-read the pinned file rather than trusting the claim
- **Why:** `evidence-index.json` files pin per-file SHA-256 for source paths, so a source change without a re-pin makes the freshness test red. Three rounds recorded this, and one round went further: two `model_layer.py`/`runner.py` digests were left unpinned, the resulting red was initially misattributed to "out-of-range parallel-session work in flight", and the correction only came from comparing the stash state. Re-pinning is also a hot-path cost — the same file was re-pinned twice in one review session.
- **Review evidence:**
  - `2026-09-16` — [cr.md:252, the convention that a source change re-pins its index records in the same round](cr.md#L252)
  - `2026-10-12` — [cr.md:360, four stale digests in the phase-0 index re-pinned, with both freshness tests green afterwards](cr.md#L360)
  - `2026-10-14` — [cr.md:677, a self-introduced red first misattributed to out-of-range parallel work, then re-pinned in the same round](cr.md#L677)
- **Fix or follow-up:** When a change touches a source file that an `evidence-index.json` pins, re-pin that digest in the same change and verify by re-reading the index, not by intention. When a freshness test fails, determine whether the change caused it (compare the stash state) before attributing it to anything else. A file pinned more than once in a short window is a signal that it is a hot path and deserves a comment, not a second fix.
- **Done right:** The freshness tests are mechanical (`tests/test_adv_review_fixes.py:1050` `test_evidence_indexes_are_fresh_and_never_index_themselves`), so the omission cannot pass CI silently — the cost of a missed re-pin is a red suite, not a wrong claim.
- **Status:** active
- **Distinct review dates observed:** 3 (2026-09-16, 2026-10-12, 2026-10-14) — meets the promotion threshold, but promotion is a separate write through the frozen-review writer with a maintainer-authorized range, so it is not performed here

### L-0003 — Before rewriting or pruning a test file, check its test ids against the evidence records that cite them
- **Why:** Four tests registered in `evidence/research-chain/release/release-record.json` had been deleted from `tests/test_research_chain_phase5.py` during an earlier "dead-code cleanup" of the same file. They were not dead code; they were live contracts cited by the release gate, and the deletion only surfaced when the gate was re-run. Deleting a test that evidence depends on converts a real check into a silent gap, which is worse than the file being longer.
- **Review evidence:**
  - `2026-10-19` — [cr.md:2111–2127, the four deleted tests found, restored verbatim from their original commits with provenance, and the mechanical check recorded](cr.md#L2111)
  - `2026-10-19` — [tests/test_research_chain_release_evidence.py:73 `test_all_phase_record_test_ids_exist` now enforces the check mechanically](tests/test_research_chain_release_evidence.py#L73)
- **Fix or follow-up:** Before any "cleanup" of a test file, enumerate its `def test_*` names and check them against `exit_criteria[].test_ids` in every phase record and release record that could reference them (a `re.search(rf"^def {name}\(", source)` check is enough). Restore from the commit that introduced a deleted test rather than rewriting it, so the original assertion stays intact. Report any test id referenced by evidence that does not exist, instead of assuming the reference is stale.
- **Done right:** The release-evidence suite already enforces the invariant mechanically, so this class of deletion now fails CI rather than waiting for a release-gate re-run.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-19) — far below the promotion threshold

### L-0004 — A quality report may only name checks it actually ran; make the check name derive from the path
- **Why:** The cross-check name in the adjusted-price quality report was hard-coded, so the `independent_restated` path published a report claiming `..._jump_matches_corporate_actions` — a check it never executed. A report that asserts an unperformed check is precisely what this gate exists to forbid, and it passes every checksum and manifest validation because the claim is only prose.
- **Review evidence:**
  - `2026-10-16` — [cr.md:979 finding 5, hard-coded cross-check name makes one path's report assert a check it did not run](cr.md#L979)
  - `2026-10-16` — [src/uq/pipeline/adjusted_price.py:296 `_report`, the check name now comes from `observed` and a missing cross-check raises instead of publishing a claim](src/uq/pipeline/adjusted_price.py#L296)
- **Fix or follow-up:** Derive report check names from the code path that produced the result, never from a constant shared across paths. When a publisher can take more than one path, add one test per path asserting the exact check name (and that an absent check cannot appear). If no cross-check ran, refuse to publish the report rather than emitting an empty claim.
- **Done right:** `QualityReportStore` already constrains the check-name taxonomy, so the fix had a natural place to land instead of inventing a parallel convention.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-16) — far below the promotion threshold

### L-0005 — Confirm the summary line exists before reporting a test count
- **Why:** An extra `-q` on top of the configured `addopts = "-q"` produced `-qq`, which suppressed the summary line. A count was recorded from a run whose summary line did not exist, so the number could not be substantiated until the run was repeated.
- **Review evidence:**
  - `2026-10-19` — [cr.md:1980, the `-qq` suppression and the instruction to confirm the summary line before reporting; the round itself spans 2026-10-18/19](cr.md#L1980)
- **Fix or follow-up:** Before quoting passed/skipped/failed counts, confirm the run actually printed its summary line. Do not add a second `-q` to a project whose `pyproject.toml` already sets `addopts = "-q"`; capture the tail of the output so the count is readable evidence.
- **Done right:** The miscount was caught within the same review and corrected before it entered any release claim.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-18) — far below the promotion threshold
<!-- ACTIVE_LESSONS:END -->

## Promotion history

> Preserve each promoted lesson's original ID, explanation, dated evidence, and follow-up here; record the promotion date and standards rule ID.

<!-- PROMOTED_LESSONS:START -->
<!-- PROMOTED_LESSONS:END -->

## Entry format

The following is a format template, not evidence and not a record to copy as a factual lesson:

```markdown
### L-0001 — <one executable rule>
- **Why:** <cause and consequence confirmed by the review; clearly mark uncertain parts>
- **Review evidence:**
  - `YYYY-MM-DD` — [specific inspected project evidence](URL-or-repository-path)
  - `YYYY-MM-DD` — [specific inspected project evidence](URL-or-repository-path)
  - `YYYY-MM-DD` — [specific inspected project evidence](URL-or-repository-path)
- **Fix or follow-up:** <fix commit, or owner if unresolved>
- **Done right:** <if any, record a counterexample or effective practice>
- **Status:** active | promoted
- **Promoted rule:** PS-0001 (required only after promotion)
- **Promotion date:** YYYY-MM-DD (required only after promotion; not evidence)
```
