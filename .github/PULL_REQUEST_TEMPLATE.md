## Goal and observable behavior

<!-- State the outcome, not just the implementation. For docs/maintenance, name the reader or operator benefit; note when runtime behavior is unchanged. Link an issue/request if relevant. -->
**Goal (one sentence):**

**Triggering condition / input:**

**Before → after (observable behavior):**

<!-- Confirm every part of the complete diff serves the stated goal; explain any necessary adjacent or incidental-looking changes. -->
**Diff-to-goal scope check:**

### Exclusion-policy change checklist

<!-- Complete this section for any change to docs/automation/range-exclusions.json or its contract. The manifest's actual excluded_paths remain the only authority; do not claim that a producer automatically excludes a path. -->
- [ ] Not applicable — this PR does not change exclusion policy or its contract.
- [ ] I explicitly list the hidden/generated content and the exact repository-relative paths affected.
- [ ] I state the old and new coverage (including paths no longer covered), the concrete producer/process or stage, and the reason for exclusion.
- [ ] I explain impact, review/verification consequences, and why code, workflow, and configuration in the same commit remain non-excluded.
- [ ] I checked that paths are explicit literals, that different sources use separate rules, and that no training output or newly added directory is automatically excluded.
- [ ] I ran the direct policy parser check: `python3 scripts/freeze-review-range.py --validate-policy docs/automation/range-exclusions.json`.

## Affected subsystems and contracts

<!-- List docs/subsystems entries. State whether interfaces, state, failure behavior, compatibility, permissions, or data formats change. -->

## Verification

- [ ] I ran the checks listed below on the commit under review.
- [ ] Regression behavior is covered by a behavior-focused test, or the limitation is explained.
- [ ] Relevant subsystem docs and project rules are updated, or this change preserves their contracts.

**Commands / CI evidence and results:**

**Not run and why:**

## Risk, migration, and owner decisions

<!-- Note secrets/permissions, data migrations, dependency/workflow changes, rollback or compatibility risks, and decisions needing maintainers. Write “None known” only after checking. -->

## Review reference

- Head SHA reviewed: <commit SHA>
- Lessons / code-standards / contributors considerations:
