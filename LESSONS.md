<!-- reviewed-through: 9e7a086cdbfb973d39db2db72beceee85f2eeddf -->
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

<!-- frozen-review-lessons: sha256:20ebdcb00d8fb5c12b677ab0e5798cd1e0b51e9748204bb1555ca55c55ed6531 -->
### L-0006 — Keep one definition of hot-path behaviour and lock that definition directly; a green suite may be pinning a copy
- **Why:** The drop-unlabeled execution segment survived both mutations (invert `keep_mask`; no-op that never drops) under a 1180-test green suite. The root cause was not a missing test but three copies of the same logic — the stage implementation, an in-test re-implementation, and a fixture that never ran — so the suite pinned the copies while the production path had zero coverage.
- **Review evidence:**
  - `2026-10-20` — [cr.md:2007 §8.57 FC-N1: the drop segment had no direct coverage and both mutations survived](cr.md#L2007) and [cr.md:2052 mutation scorecard M4a/M4b both survived](cr.md#L2052); fix commit `54676a0` extracted `drop_unlabelled_factor_rows` as the single definition and locked it with `TestDropUnlabelledFactorRows`
- **Fix or follow-up:** Landed in `54676a0`. When adding behaviour on a hot path (dataset policy execution, writer invariants), check that the logic exists exactly once and that the test imports and pins that definition instead of re-writing a parallel copy.
- **Done right:** The fix verified both mutation directions (inverted mask and no-op mask) and recorded the mutation-verification claim in the commit message itself.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0007 — Replay resume paths in a fresh process; same-process green does not prove fresh-process recovery
- **Why:** Research-chain stages hand results between adapters through `adapter.last_result`, an attribute no adapter ever initialized, so a fresh-process resume crashed with AttributeError at every boundary after the model stage. Every existing resume test ran inside a single process and never reached the missing-attribute branch, which made the gap structurally invisible.
- **Review evidence:**
  - `2026-10-20` — [cr.md:2016 §8.57 FC-N2: the `last_result` hand-off breaks fresh-process resume at every post-model boundary](cr.md#L2016); fix commit `54676a0` declares the attribute on the four adapters and rebuilds the hand-off from the published manifest and artifact store, the same read-back path the model stage uses after training
- **Fix or follow-up:** Landed in `54676a0`. When adding a cross-stage hand-off, check that it has an on-disk read-back path and that resume tests kill and restart the process instead of chaining stages in one interpreter.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0008 — Verify syntax and behaviour on the minimum declared Python version; a green run on a newer interpreter does not cover the declared range
- **Why:** `56d9750` landed a nested same-quote f-string (`f"{stage_plan_for_request({"contract_version": 1}).index(stage):02d}"`, a PEP 701 form that only parses on Python 3.12+) while `pyproject.toml` declares `requires-python >= 3.11`. The local 3.12 environment stayed green and the defect surfaced only when the 3.11 path was exercised; `e4d7e45` rewrote it the same day.
- **Review evidence:**
  - `2026-10-20` — [tests/test_paper_execution_research_integration_phase0.py, the file whose 3.12-only f-string e4d7e45 rewrote for 3.11](tests/test_paper_execution_research_integration_phase0.py); commits `56d9750` (introduced) and `e4d7e45` ("support python 3.11 stage paths"), both 2026-09-10, verified from the diffs
- **Fix or follow-up:** Landed in `e4d7e45`. The certified range is 3.11/3.12/3.13; before treating a change as certified, either exercise it on the 3.11 floor (a `compileall` of the touched files is enough for syntax) or wait for the remote matrix instead of trusting the newest local interpreter.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0009 — Gate optional-dependency tests at the import layer, and grep for sibling instances before closing one
- **Why:** `72b35ce` gated the phase-3 Qlib test "independently", but the gate sat on a helper that itself hard-imported qlib, so the gate was dead and collection still crashed in qlib-less cells. The same class had to be fixed again the same day: `7ee7a8f` moved the phase-5 gating to the import layer ("gate qlib tests without hard import") and re-pinned the affected gate report.
- **Review evidence:**
  - `2026-10-20` — [tests/test_research_chain_phase5.py, import-layer gating landed by 7ee7a8f](tests/test_research_chain_phase5.py) and [tests/test_research_chain_phase3.py, where the first (dead) gate of 72b35ce sat](tests/test_research_chain_phase3.py); both commits dated 2026-09-05
  - `2026-10-20` — Correction from same-day re-review: the dead helper-gate described in **Why** was in [tests/test_research_chain_phase5.py `_run_full_chain`](tests/test_research_chain_phase5.py) (a `skipif` decorator on a non-test helper, inert, plus a module-level hard import of `QlibRuntimeTrainer`), fixed by 7ee7a8f; 72b35ce's phase-3 change was already the correct import-layer pattern (in-test `pytest.importorskip("qlib")`, still present at line 119 after 7ee7a8f), so 72b35ce was not itself defective — the class still needed two same-day sweeps
- **Fix or follow-up:** Landed in `72b35ce` + `7ee7a8f`. Optional-dependency gating belongs where the import happens (`pytest.importorskip` / `find_spec` before any import of the optional package), never on a helper downstream of a hard import. When fixing one mis-gated test, grep the suite for the same pattern before calling the class closed.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0010 — Tests may only depend on the locked environment; never shell out to host binaries
- **Why:** The phase-5 audit test shelled out to the host's `rg`. `b0614dc`'s own message records the blast radius: "Phase 5 audit invoked external rg, absent in CI; all ten runtime cells failed on the test dependency." The certified environment is `uv.lock` plus the runner image; a host binary is outside both, so the failure only appeared on the remote matrix.
- **Review evidence:**
  - `2026-10-20` — [tests/test_paper_execution_phase5.py, the audit made portable by b0614dc](tests/test_paper_execution_phase5.py); commit `b0614dc` (2026-09-09), whose message carries the all-ten-cells failure summary quoted above
- **Fix or follow-up:** Landed in `b0614dc`. A test that needs search/filter capability implements it in Python against locked dependencies; treat any `subprocess` call to a non-Python binary in tests as a certification blocker.
- **Done right:** The same commit archived the failed remote run as `evidence/paper-execution/release/final-cr/remote-failed-run-34353302888.json` instead of discarding it — failed evidence was preserved as evidence.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0011 — Every check an evidence record cites must exist and run in the run that produced the evidence
- **Why:** The paper-execution release evidence archived from run 38252277339 cited a lock test that only landed in `a76e89e` — the certified matrix that produced the evidence had never executed one of the checks the record relied on. The whole evidence set had to be superseded: `d459006` re-ran the gate at `a76e89e` (run 38253299816) and rebound the release record, per its own message.
- **Review evidence:**
  - `2026-10-20` — [evidence/paper-execution-research-integration/release/release-record.json, the record rebound by d459006](evidence/paper-execution-research-integration/release/release-record.json); commits `a76e89e` and `d459006` (2026-10-12), the latter's message stating the lock test landed in `a76e89e` and superseding run 38252277339
- **Fix or follow-up:** Land the gating test first, run the gate, and only then archive evidence that cites it. If a record cites check X, the cited run must be one where X existed; when in doubt, re-run rather than re-word.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0012 — Bind evidence to the last code-affecting commit, not to a moving HEAD; exempt docs-only drift explicitly
- **Why:** After the A500 pilot archive (`23cb49a`), the evidence binding chased HEAD through a dozen bind/rebind commits on 2026-10-11/12 (`55c478a`, `54a50aa`, `acd1eea`, `709ccd3`, `21312e5`, `be672b8`, `9d9aeb4`, `2c54f80`, `4347d52`, `c1ddc55`, `609a32f`, `5cbe7f7`), because each new code commit invalidated the recorded `git_commit`; a full gate re-run was even spent on `6facb19`, a one-line docs-only commit. Binding to a moving target turns an evidence record into a race.
- **Review evidence:**
  - `2026-10-20` — [evidence/paper-execution-research-integration/release/release-record.json, the target of the binding chase](evidence/paper-execution-research-integration/release/release-record.json); the commit chain verified via `git log 35c4e1b..7970eec`, and `6facb19` confirmed as a one-line `pendding.md` change from its own stat
  - `2026-10-20` — Correction from same-day re-review: the clause "a full gate re-run was even spent on 6facb19" in **Why** is not supported by any located evidence (no commit message or evidence artifact ties a gate run to that commit; [6facb19 is confirmed one-line docs-only](pendding.md)); the verified core is the twelve-commit binding chase
- **Fix or follow-up:** Archive evidence only once the code shape has settled; record the last code-affecting commit as the binding and state explicitly that later docs/evidence-only commits do not invalidate it. If HEAD must move, define which path classes require re-binding (code, schemas, workflows) and which do not.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0013 — Registration of an optional comparison component must match its optional semantics; fake-adapter tests cannot see real connection costs
- **Why:** `31af0d9` wired the TDX comparator into the v2 builder's adapter map unconditionally, and the router selects every registered `compare_with` source, so each v2 run paid the TDX multi-server connect/retry cost although the default path does not enforce the comparison. `29514dd`'s message carries the measurement: the same five sessions exceeded 150 s and had to be killed with TDX registered, versus 2.4 s total unregistered. No fake-adapter test could surface this, because fakes never open sockets.
- **Review evidence:**
  - `2026-10-20` — [src/uq/pipeline/daily.py, where 29514dd made comparator registration conditional on cross-validation being required](src/uq/pipeline/daily.py); commits `31af0d9` and `29514dd` (2026-10-11), measurement quoted from the fix message
- **Fix or follow-up:** Landed in `29514dd`. Default registration must follow the feature's default semantics (off unless required). For components whose cost is connection or IO, run one measured smoke of the real registration path before release; mocked adapters structurally cannot surface connection cost.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0014 — Skip gates must sit on the tested code's real import chain, and wiring assertions must hit the instance actually wired
- **Why:** `d62f419`'s CLI wiring test skipped only when `joblib` was absent, but `_build_runner` imports the Qlib-backed trainer — base CI cells install joblib and not qlib, so the test ran exactly where it had to fail. The same test also constructed `run_store = FileResearchRunStore(tmp_path)`, passed a *different* instance into `_build_runner`, and asserted on the decoy. `7eecc3c` fixed both: the gate now also requires qlib (matching the sibling provider-wiring test), and the assertion is `runner.dataset_adapter.membership_store is store` on the object actually passed.
- **Review evidence:**
  - `2026-10-20` — [tests/test_research_membership_input.py `test_cli_build_runner_wires_membership_governance_into_the_dataset_stage`, both defects visible in the d62f419 version and fixed in 7eecc3c](tests/test_research_membership_input.py); commits `d62f419` and `7eecc3c` (2026-10-12), verified from the diffs
- **Fix or follow-up:** Landed in `7eecc3c`. Before choosing a skip guard, trace the import chain of the unit under test and gate on the full set of optional packages it touches (match the sibling pattern, e.g. `@requires_qlib`). In wiring tests, assert identity (`is`) against the object passed in, never against a separately constructed look-alike.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0015 — Tile review ranges seamlessly, and after parallel sessions diff the committed tree against the pre-verified state
- **Why:** §8.17 covered `7970eec..11a11c9` and §8.22 started at `b81b44a`; the three commits in between — `cd7cf10` (closes eight external-review findings), `4bb29b1` (closes nine second-pass CR findings), and `b81b44a` (the very commit recording §8.17) — fell into no review range. The hole is mechanically verifiable from the two section headers plus `git log`. Inside it, two additions carried into `4bb29b1` from a parallel session had been mutation-verified only in the pre-commit worktree, and the committed tree was never diffed against that verified state (as recorded in the round's own notes), so pre-commit verification did not provably cover what landed.
- **Review evidence:**
  - `2026-10-20` — [cr.md:565 §8.17 header, range 7970eec..11a11c9](cr.md#L565) and [cr.md:656 §8.22 header, range b81b44a..HEAD](cr.md#L656); `git log 11a11c9..b81b44a` returns exactly `cd7cf10`, `4bb29b1`, `b81b44a`
  - `2026-10-20` — Correction from same-day re-review: [cr.md:587](cr.md#L587) records that the supervisor's six own fixes were mutation-killed pre-commit, while the two parallel-session additions in 4bb29b1 received line review plus the post-commit suite but no mutation-kill verification; the **Why**'s phrasing ("mutation-verified only in the pre-commit worktree") misstated that record
- **Fix or follow-up:** When reviews proceed in ranges, the next round's `from` must equal the previous round's `through` — the frozen-review machinery now enforces this via the watermark, and this lesson is the historical evidence for why it must. When a commit carries content prepared in a parallel session, diff the committed tree against the verified worktree state before claiming the pre-commit verification covers it.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0016 — Fix-round ledger entries need a dedicated traceability pass: spec anchors, counts, cross-references, status sentences
- **Why:** The §8.24/§8.25 closure rounds document the ledger-defect class found when the §8.22/§8.23 fix-round records were traced: CR22-2's spec anchor pointed at §7.2 while the cited fail-closed rule lives in §7.3, and OBS-3's "4 new locks killed" understated a real count of 6 — prose drift that code review structurally cannot catch. The same class surfaced again in this review: §8.12's header claims "全部 61 提交" over `2e45e19..7970eec`, which `git rev-list --count` returns as 59 (60 even counting both endpoints).
- **Review evidence:**
  - `2026-10-20` — [cr.md:663 CR22-2, the spec-anchor misreference fixed in §8.24](cr.md#L663) and [cr.md:731 OBS-3, the mutation-count misstatement corrected in §8.25](cr.md#L731), with the closure rounds at [cr.md:702 §8.24](cr.md#L702) and [cr.md:723 §8.25](cr.md#L723)
  - `2026-10-20` — [cr.md:455 §8.12 header claiming 61 commits](cr.md#L455) versus `git rev-list --count 2e45e19..7970eec` = 59, re-derived in this review
- **Fix or follow-up:** After any fix round, run one pass whose only job is ledger fidelity: re-derive every count with git, open every cited anchor, and re-read every quoted verdict against its source. This class is distinct from L-0005 (run-output counts): it concerns references and statements inside the ledger itself.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)
<!-- /frozen-review-lessons: sha256:20ebdcb00d8fb5c12b677ab0e5798cd1e0b51e9748204bb1555ca55c55ed6531 -->

<!-- frozen-review-lessons: sha256:8db5779f2b04ae9ece78f57e62028682f122b0b99fdcee7031b1c30f51dad1c2 -->
### L-0017 — An empty probe says nothing about the source until the request construction is verified with a positive control
- **Why:** §8.43 observed TDX `xdxr` returning 0 rows for BSE instruments, concluded "TDX does not provide BSE events", and designed, built and documented a per-instrument dividend-store fallback on that premise. §8.44 overturned the premise: mootdx's `get_stock_market` routes leading-`9` codes via `('5','6','9','7') → 'sh'` (market=1) *before* the `('4','8') → 'bj'` branch, so `920xxx` was queried against the wrong market — `get_xdxr_info(2, "920000")` returns 51 rows. The misdiagnosis survived because no positive control was consulted: old NEEQ segments (`43xxxx`/`83xxxx`) routed correctly and returned data, so "the source has these events for some codes" was already observable. The cost of the unverified premise was amplified by building architecture (a flag, a fallback path, tests, four doc sections) on top of it before re-checking it. This is the same class as the §8.30 probe discipline ("probe with the real command you will send"), one layer deeper: the command family was right, the derived parameters were not.
- **Review evidence:**
  - `2026-10-20` — [cr.md:1299–1329 §8.44: the §8.43 fallback built on the wrong premise and its overturn, the routing bug at :1313, the positive measurement at :1315, the self-correction at :1323](cr.md#L1299)
  - `2026-10-20` — [cr.md:1325, the record's own lesson sentence, explicitly linked to the §8.30 probe discipline as the same class](cr.md#L1325)
- **Fix or follow-up:** Landed in §8.44 — `MootdxSourceAdapter.market_for()` parses prefixes explicitly (`92`/`43`/`83`/`87`/`88` → market 2, unknown prefixes refuse instead of defaulting), `fetch_xdxr_events` uses `get_xdxr_info(market, symbol)`, and `tests/test_mootdx_source.py` locks the routing. Rule: before attributing an empty result to the vendor, re-run the probe against an object known to exist (positive control) and verify every derived request parameter against the vendor's own routing code. A "source has no data" finding that would justify new architecture must have its premise confirmed twice before anything is built on it.
- **Done right:** The maintainer's challenge "没用错参数吧" was exactly the falsifying question the round had skipped; §8.44 recorded the self-correction explicitly, kept `--dividend-root` only with its corrected justification, and rewrote the four doc sections instead of leaving the old rationale in place.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0018 — A test whose fixture never drives execution through the target branch is fake-green; prove teeth with one disabling mutation
- **Why:** Twice in the same week a first-version test passed while locking nothing, and both were exposed only by mutation: FC-M7's suspension-side test used an all-NaN panel, so the instrument was skipped before the suspension branch and no fill row ever existed (the side-flip mutation survived); the BSE fallback test used the default `600000`/`000001` instruments, so the assertion held vacuously over an always-empty array (the fallback-disabling mutation survived). Both were self-registered as 返工 and rewritten so the fixture forces control flow through the branch under test.
- **Review evidence:**
  - `2026-10-20` — [cr.md:1222, FC-M7 suspension-side first test fake-green on an all-NaN panel, self-registered rework, fixed by declaring the suspension with valid prices](cr.md#L1222)
  - `2026-10-20` — [cr.md:1293, BSE fallback fixture asserting on an always-empty array with default instruments, self-registered fixture rework, fixed with `920000.XBSE`](cr.md#L1293)
- **Fix or follow-up:** Landed — FC-M7 declares the suspension via `suspension_dates` with valid prices so a fill row is actually produced, and the BSE test uses `920000.XBSE` so the asserted array is non-empty. Rule: a branch-targeting test must assert on an artifact only that branch can produce (a fill row, a non-empty collection), and the cheapest teeth-proof is one mutation that disables the branch — if the mutation survives, the fixture, not the code, is wrong.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)

### L-0019 — A threshold or guard that can reject real data must be validated against the real data distribution before it ships
- **Why:** §8.36 wrote the adjusted-price jump tolerance as a pure relative 1e-3; the exchange rounds ex-rights reference prices to the tick, so the legitimate residual is ≈ `tick / previous_close` — a 3-yuan instrument exceeds budget and would have its whole session refused, and the real all-A window already had 600858's residual at 89% of budget. The defect was pushed in `9c811ac` and caught by the §8.37 self-review, which reworked the gate with the physical floor. Same shape in FC-M3: wiring `assert_not_raw_close` into production as a blanket guard would have rejected 62/5059 instruments in one real session whose factors are legitimately constant 1.0 (never had a corporate action) — a fixture invariant mistaken for a data invariant, reverted after the empirical check.
- **Review evidence:**
  - `2026-10-20` — [cr.md:1052–1064 §8.37 CR37-1: the pure-relative tolerance introduced in §8.36, pushed, caught by self-review, reworked with the `tick / previous_close` floor](cr.md#L1052)
  - `2026-10-20` — [cr.md:1214 FC-M3: the blanket `assert_not_raw_close` production guard reverted after the 62/5059 real-session check, recorded as characterized-not-fixed](cr.md#L1214)
- **Fix or follow-up:** Landed — the jump gate floors tolerance at `tick / previous_close` and the helper is docstring-scoped to tests with the 62-instrument counterexample recorded. Rule: before shipping a refusal threshold or guard, run it over a real window and inspect the worst-case legitimate subjects (lowest-priced names, no-corporate-action names); derive numeric bounds from the domain's physics (tick size, grid resolution), not from what "looks tight" or from fixture semantics.
- **Done right:** CR37-1's fix was verified by re-running the full all-A window and showing both partition generation ids byte-identical — the fix widens the gate without rewriting published bytes; FC-M3's outcome was recorded as "如实定性而非修好", a reverted fix downgraded honestly instead of being claimed closed.
- **Status:** active
- **Distinct review dates observed:** 1 (2026-10-20)
<!-- /frozen-review-lessons: sha256:8db5779f2b04ae9ece78f57e62028682f122b0b99fdcee7031b1c30f51dad1c2 -->
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
