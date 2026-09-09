# Paper Execution Research Integration Plan

Status: **v0.1 contract-first; phases paused until Phase 0 exits**

This plan extends the released Research Chain orchestrator without modifying
the released `research_run_request.v1`, `research_run_request.v2`, or stage
plan `v1/v2`. It adds a new request schema version and stage plan version so
the released chain remains byte-for-byte compatible.

## 0. Goal

Add the released Paper Execution Layer as the next governed stage:

```text
Portfolio Stage
  -> Backtest Stage
  -> Paper Execution Stage
  -> Research Result Reconciliation
```

The stage consumes only published `target_weights.v1` partitions plus governed
market/calendar/suspension/corporate-action inputs. It uses the existing
`PaperOrderPlanner`, `PaperExecutionEngine`, `PaperPortfolioStateBuilder`, and
their immutable stores. It must not create paper semantics, broker access, live
market data, or execution logic in the Research Chain.

## 1. Non-Goals

- No broker connectivity or submission-only lifecycle records.
- No live market data, streaming, credentials, or network calls.
- No replacement of backtest execution.
- No reimplementation of order planning, execution, or state evolution.
- No modification of released `research-chain-layer v0.8` stage identities.
- No reuse of failed or staging outputs as accepted paper inputs.
- No bypass of quality reports, trust anchors, manifests, or readback checks.

## 2. Contract Changes

### 2.1 Research request v3

Add `research_run_request_v3.json`. It must be derived from v2 and add:

- `execution_config_template.v1` with mode `paper`, market `cn_a`,
  state mode policy, lot size, fees, limits, volume guard, cash/tolerance,
  serialization profile, and reviewed status.
- explicit bindings for execution market data, calendar, suspension,
  corporate-action exclusion, and initial paper state.
- a `paper_execution_risk_policy_binding` for the required pre-trade risk
  decision.
- the externally reviewed `stage_plan_v3_activation` review.

The request schema must remain closed. No free-form template fields are allowed.

### 2.2 Stage plan v3

The normative stage plan is:

1. `resolve_request`
2. `factor_computation`
3. `dataset_preparation`
4. `qlib_export`
5. `model_training`
6. `prediction_publication`
7. `portfolio_construction`
8. `backtest_execution`
9. `paper_execution`
10. `result_reconciliation`

The digest must be exposed by `research_stage_plan_v3_sha256()` and covered by
a golden vector. Only a `research_stage_plan_v3_activation` review with the
exact subject generation and manifest digest can activate the v3 request.

### 2.3 Stage outputs

A successful paper stage publishes, in order, for each target-weight decision
date:

1. verified order plan;
2. verified execution result;
3. verified next paper portfolio state.

Each publication must use the owning paper execution stores and must bind the
external reviewed quality decision for that family. The stage state records an
ordered binding list. No single merged pseudo-artifact may replace these
immutable outputs.

## 3. Phases

### Phase 0 — Contract basis

Deliverables:

1. `research_run_request_v3.json` and typed loader registration.
2. `research_stage_plan_v3_sha256()` and activation-review validation.
3. Explicit closed validation for execution templates and upstream bindings.
4. Valid/invalid representative fixtures and golden vectors.
5. Negative tests for malformed request, wrong review signature, wrong stage
   digest, missing execution binding, and unknown execution template field.

Entry criteria: Paper Execution Layer and Research Chain v0.8 are released.

Exit criteria:

1. Unified gate passes.
2. v1/v2 request loading and stage behavior remain unchanged.
3. v3 request cannot validate with a v2 stage plan digest or review type.
4. All Phase 0 fixtures are persisted and indexed.

### Phase 1 — Runtime resolver

Deliverables:

1. Accept only the reviewed v3 stage plan.
2. Resolve market, calendar, suspension, corporate-action, initial state, and
   risk-policy bindings.
3. Validate visibility dates and rejected missing/tampered inputs.

Exit criteria:

1. Resolver produces an ordered plan containing `paper_execution`.
2. Missing or tampered upstream documents fail closed before computation.
3. v1/v2 resolver tests remain green.

### Phase 2 — Adapter

Deliverables:

1. `PaperExecutionStageAdapter` invokes only owning execution APIs.
2. For every target-weight decision date, produce plan, result, and next state.
3. Maintain previous-state linkage across dates.
4. Publish stage state only after successful readbacks.

Exit criteria:

1. Initial and continuation paths are covered.
2. Multiple decision dates preserve ordered state lineage.
3. Any failed date stops the stage and excludes all later outputs from success.

### Phase 3 — Result reconciliation

Deliverables:

1. Extend successful research result to bind all paper stage outputs.
2. Reject missing, extra, unordered, duplicated, or tampered bindings.
3. Preserve research run state append-forward semantics.

Exit criteria:

1. A successful result cannot omit paper evidence.
2. Re-reads verify generation, manifest digest, data checksum, quality report,
   and physical path for every paper artifact.
3. Failure-state output remains invisible to accepted result publication.

### Phase 4 — Release evidence

Deliverables:

1. Local base and Qlib gates.
2. Remote ten-cell unified matrix.
3. Release marker, phase record, evidence index, and immutable historical
   evidence if any defect is reconciled.

Exit criteria:

1. All prior phases exited.
2. Remote matrix passes on the final implementation HEAD.
3. Release evidence is indexed with SHA-256 digests.
4. No acceptance row is `TBD` or unimplemented.

## 4. Acceptance Matrix

| ID | Phase | Test focus | Status |
|---|---|---|---|
| PE1 | 0 | v3 schema/loader validation | `pending` |
| PE2 | 0 | v3 stage-plan digest sensitivity | `pending` |
| PE3 | 0 | invalid activation review rejected | `pending` |
| PE4 | 1 | market binding resolution and visibility | `pending` |
| PE5 | 1 | tampered/missing input fail closed | `pending` |
| PE6 | 2 | one-date initial paper execution | `pending` |
| PE7 | 2 | multi-date continuation state evolution | `pending` |
| PE8 | 2 | suspension, limit, cash, and T+1 failures preserved | `pending` |
| PE9 | 3 | result reconciliation and tamper rejection | `pending` |
| PE10 | 4 | local and remote release gates | `pending` |

No row may be moved to `passed` without an exact persisted test and evidence
path.

## 5. Execution Rules

1. Every phase needs local unified gate evidence before exit.
2. Phase 4 also requires remote ten-cell CI evidence.
3. Any semantic change reopens the relevant phase and invalidates later
   evidence.
4. The runner may only call owning layer stores and engines; it never constructs
   paper artifacts or signs quality reports.
