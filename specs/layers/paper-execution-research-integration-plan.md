# Paper Execution Research Integration Plan

Status: **Phase 0 (v3 contract basis) exited at `de26458`; Phase 0R contract
remediation implemented; Phase 0.5 next. Runtime phases 1-4 stay paused until
Phase 0R exits with gate evidence and Phase 0.5 exit.** The released
`research_run_request.v1`/`.v2` contracts and stage plans `v1`/`v2` remain
byte-for-byte compatible; nothing here changes them.

This plan extends the released Research Chain orchestrator so the released Paper
Execution Layer runs as a governed stage. It is contract-first: no execution path opens
until the contract basis and the governed input publishers exist.

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
`PaperOrderPlanner`, `PaperExecutionEngine`, `PaperPortfolioStateBuilder`, and their
immutable stores. It must not create paper semantics, broker access, live market data,
or execution logic in the Research Chain.

## 1. Non-Goals

- No broker connectivity or submission-only lifecycle records.
- No live market data, streaming, credentials, or network calls.
- No replacement of backtest execution.
- No reimplementation of order planning, execution, or state evolution.
- No modification of released `research_run_request.v1`/`.v2` or stage plan `v1`/`v2`.
- No reuse of failed or staging outputs as accepted paper inputs.
- No bypass of quality reports, trust anchors, manifests, or readback checks.
- No production-runtime support in this slice. The paper stores accept runtime mode
  `research` or `production`, but every stage-plan review trust anchor in
  `config/research-stage-plan-review-anchor.*.json` is a repository test key, which
  `uq.runtime.require_production_review_key` rejects. Paper research runs are therefore
  supported only in `research` mode until an external signing ceremony publishes
  non-test anchors.

## 2. Contract Changes

### 2.1 Version register (what exists, what is superseded, what is executable)

| Artifact | State | Executable |
|---|---|---|
| `research_run_request.v1`, `.v2` | released | yes (current chain) |
| `research_run_request_v3.json` + `paper_execution_template.v1` | Phase 0 contract basis, indexed | **no**, superseded before ever running |
| `research_run_request_v4.json` | v3 plus `index_membership_binding` and `factor_partition_set_binding` (pendding 4a/4d) | **no**, superseded |
| `research_run_request_v5.json` + `paper_execution_template.v2` | **Phase 0R deliverable** | yes, once Phase 2 lands |

v3 and v4 cannot express an executable paper run: `paper_execution_template.v1` carries
no initial paper state and no opening cash, while `execution_config.v1` requires
`initial_state` (`holdings`, `cash`, `valuation_price_basis`, `provenance_sha256`) and
both `PaperOrderPlanner.plan()` and `PaperExecutionEngine.execute()` require
`opening_cash`. Editing v3/v4 in place would change the semantics of already-indexed
durable contracts, so the widening lands as a new version (the same rule used for the
`adjustment_snapshot` v2 to v3 XBSE widening).

v3/v4 files, fixtures, anchors and digests stay published and readable. Their runtime
stays refused fail-closed, and `stage_plan_for_request` keeps mapping {1,2} to the V1
order and {3,4,5} to the ten-stage order.

### 2.2 `paper_execution_template.v2`

New closed schema; v1 is not edited. Changes from v1:

- `mode` (`paper`) and `market` (`cn_a`) become explicit template fields instead of
  being implied by the request;
- `initial_state`: `{cash, holdings[], valuation_price_basis}` for the first decision
  date, or a governed binding to a published `paper_portfolio_state.v1` generation when
  a run continues an existing book. Exactly one of the two, enforced closed;
- the four input bindings become **partition-set bindings** of the shape
  `{family, window_start, window_end, set_digest_sha256, member_count}`:
  `market_data_binding` (bars partition set), `calendar_binding` (calendar snapshot),
  `suspension_binding` (suspension snapshot set), `corporate_action_binding` (dividend
  event partition set);
- `risk_policy_binding` names the reviewed `risk_policy.v1` generation for the run. The
  per-decision-date `risk_decision.v1` documents are produced at runtime (2.5), so a
  single run-level decision binding is no longer part of the contract;
- retained unchanged from v1: `template_version`, `status: reviewed`,
  `state_mode_policy`, `lot_size`, `quantity_policy`, `fee_policy`, `price_policy`,
  `data_delay_policy`, `data_delay_reason`, `serialization_profiles`.

Binding identity convention (resolves the three incompatible binding shapes found in
the R1 review): every input binding carries `set_digest_sha256` over ordered
`{session_or_decision_date, generation_id, data_checksum_sha256}` triples. That is the
same shape as `factor_partition_set_binding` in v4, and it is verifiable against the
existing manifest-first readers (`ManifestFirstReader`,
`BacktestMarketDatasetStore.read_frame`, `DividendEventStore.read_events`), which key on
`data_checksum_sha256` rather than on a manifest digest those manifests do not carry.

### 2.3 `research_run_request_v5`

Derived from v4 unchanged except: `contract_version` and `schema_version` consts 5 and
`5.0.0`, `paper_execution_template` `$ref` points at template v2, and the v3 activation
review becomes `research_stage_plan_v5_activation`. The stage plan digest is the
ten-stage order of 2.4 tagged `{"schema_version": "v5"}`, exposed as
`research_stage_plan_v5_sha256()` with
`config/research-stage-plan-review-anchor.v5.json`.

### 2.4 Stage plan (v3/v4/v5 share the ten-stage order)

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

Each version's digest is version-tagged, so v3, v4 and v5 digests differ while the
order is identical. Persisted golden vectors are required in Phase 0R because the v3
digest is today only re-derived inside tests:
`evidence/paper-execution-research-integration/phase-0r/golden/research-stage-plans.json`.

### 2.5 Per-decision-date risk decision flow

`ExecutionConfigStore.publish` requires a `ConfigPublicationBinding` for a
`risk_decision.v1` document whose `binding_config_generation_id` equals the config's
stable content generation, whose `as_of_date` equals the config `decision_date`, whose
action is `allow` or `warn`, and which is consumed once. Therefore:

- the resolver requires a reviewed risk policy generation and resolves the risk run and
  state bindings from the request `risk_binding`;
- for every target-weight decision date the adapter asks the risk control plane for one
  order-submission decision bound to that date's config generation
  (`paper_execution_identities` excludes `quality_report_checksum_sha256` for paper
  families, so that generation is stable across report binding);
- each decision is published to the risk decision store before the config publication
  consumes it, so the evidence is durable and replayable;
- a refused or expired decision for date D stops the stage at D, writes the failed stage
  state, and publishes nothing for dates after D.

### 2.6 Quality check ownership

The review registry pins exactly four checks per paper family (`execution_config_v1`,
`order_plan_v1`, `execution_result_v1`, `paper_portfolio_state_v1`; policy `reject_all`,
allowed status `passed`) and `_validate_exact_quality_checks` requires the report to
carry exactly those. The observations are today assembled only inside `tests/`. Phase 0R
moves that logic into the paper layer as a production helper
(`src/uq/execution/quality_checks.py`) which the adapter calls; the runner still never
signs a report, which stays the external reviewer's job.

### 2.7 Stage outputs

For each target-weight decision date a successful paper stage publishes, in order:

1. `execution_config.v1`;
2. verified order plan;
3. verified execution result;
4. verified next paper portfolio state.

Each publication binds the external reviewed quality decision for that family, and the
stage state records the ordered binding list. No merged pseudo-artifact may replace
these immutable outputs. `execution_id` is derived as
`sha256(canonical_json({request_content_generation_id, run_id, decision_date}))` so the
paper store partition paths (`execution-configs/<execution_id>/...`) are deterministic
across reruns and cannot collide between runs.

### 2.8 Released state/result families accept the new version

`research_run_state.v1` and `research_run_result.v1` declare `contract_version: [1, 3]`.
The runner copies the request version into both documents, so accepting v5 requires
widening those enums to `[1, 3, 4, 5]`. Widening an accepted-input enum is
compatibility-preserving: it must not change the validation or identity of
already-published v1/v3 documents (the same widening was applied at `9db45dc` when v3
landed). Phase 0R locks that with a regression test asserting the v1 and v3 state and
result documents still validate and hash unchanged.

## 3. Phases

### Phase 0 - Contract basis (v3) - exited

Exited for the v3 contract basis at `de26458`, with ten-cell remote evidence under
`evidence/paper-execution-research-integration/phase-0/remote-final-head/` and
acceptance rows PE1-PE3 in `phase-0/phase-record.json`. The R1 review found that basis
incomplete (no golden vectors on disk, no initial state or cash carrier, no set
bindings). Those findings are Phase 0R work, not a reopened Phase 0.

### Phase 0R - Contract remediation (currently open)

Deliverables:

1. `paper_execution_template.v2.json` and `research_run_request_v5.json` with typed
   loader and family registration; `research_stage_plan_v5_sha256()` plus
   `config/research-stage-plan-review-anchor.v5.json` and signing support in
   `scripts/sign_stage_plan_review.py`.
2. `research_run_state.v1`/`research_run_result.v1` enum widening with the compatibility
   regression lock (2.8).
3. A set-binding resolver helper that recomputes `set_digest_sha256` from governed
   stores, shared with the existing `index_membership_binding` and
   `factor_partition_set_binding` verification in
   `src/uq/research_chain/membership_input.py`.
4. Persisted representative and negative fixtures for template v2, request v5, and each
   set binding: at least two distinct negatives per family (schema rejection and
   manifest tampering), plus the stage-plan golden vector file (2.4).
5. `src/uq/execution/quality_checks.py` (2.6) with unit tests.
6. The v3/v4 runtime-refusal lock kept, extended to assert v5 is the only
   paper-executable version.

Exit criteria:

1. Unified gate passes and the ten-cell remote matrix passes on the final HEAD.
2. v1/v2/v3/v4 contract behaviour unchanged (golden digests unchanged).
3. Template v2 and request v5 cannot validate with a v1 template, a v3/v4 stage plan
   digest, or a v3/v4 review type.
4. All Phase 0R fixtures, golden vectors, and phase record are persisted and indexed.

### Phase 0.5 - Governed market dataset publisher (new)

This plan previously assumed governed calendar/suspension/corporate-action inputs. They
exist as binding names only: `BacktestMarketDatasetStore` is read-only
(`src/uq/research_chain/owning_contracts.py`) and the only writer of its physical layout
is a test helper (`tests/test_research_chain_phase4.py:243`). Real runs need publishers.

Deliverables:

1. `trading_calendar_snapshot.v1`: sessions published from the vendor calendar with the
   vendor-independent EastMoney index-session cross-check that landed in `e26e17b`
   (`src/uq/adapters/eastmoney.py`), storing per-session rows plus provenance and the
   independent-calendar agreement result in the manifest.
2. `suspension_snapshot.v1`: per-instrument/per-session state derived from governed
   `bars_daily.v2` `status`, reconciled against Tushare `suspend_d`. The bars `status`
   column is the primary source; `suspend_d` is recorded as reconciliation provenance,
   not a second truth.
3. Corporate-action exclusion set derived from `DividendEventStore` partitions
   (`data/dividend-store`), published as an ordered partition-set manifest so
   `set_digest_sha256` is recomputable.
4. One `MarketDatasetPublisher` store family under the canonical store root with
   manifest-first publish, overwrite rejection, no-symlink path policy, and a reader
   that returns ordered `{date, generation_id, data_checksum_sha256}` triples.
5. Script entry points so a pilot run can rebuild these inputs reproducibly (Tushare 2K
   token plus EastMoney; no new vendor).

Exit criteria:

1. Every Phase 0R input binding resolves against published artifacts and each recomputed
   `set_digest_sha256` matches the bound value.
2. Tampering any member row or artifact flips the set digest and is rejected.
3. Windows whose start precedes the first visible calendar snapshot fail closed.
4. Evidence archived under `evidence/paper-execution-research-integration/phase-0.5/`
   with gate reports and phase record.

### Phase 1 - Runtime resolver

Deliverables:

1. Accept the reviewed v5 stage plan; keep v3/v4 refused.
2. Resolve market, calendar, suspension, corporate-action, initial state, and risk
   policy bindings against published stores, recomputing set digests.
3. Validate visibility dates (window sessions inside the calendar snapshot; market data
   visible before the decision timestamp; risk policy and initial state visible at the
   window start) and reject missing or tampered inputs before any computation.
4. Keep v1/v2 resolver behaviour unchanged.

Exit criteria:

1. Resolver produces an ordered plan containing `paper_execution`.
2. Missing or tampered upstream documents fail closed before computation.
3. v1/v2/v3/v4 resolver tests remain green.

### Phase 2 - Adapter

Deliverables:

1. `PaperExecutionStageAdapter` invoking only owning execution APIs.
2. Per decision date: risk decision (2.5), config, plan, result, next state.
3. Previous-state linkage across dates; the first date uses the template initial state.
4. Stage state published only after successful readbacks of all four publications.

Exit criteria:

1. Initial and continuation paths are covered.
2. Multiple decision dates preserve ordered state lineage.
3. Any failed date stops the stage and excludes all later outputs from success.
4. v5 execution is no longer refused, and the refusal lock becomes an enabled-path lock.

### Phase 3 - Result reconciliation

Deliverables:

1. Extend the successful research result to bind all paper stage outputs (4N bindings).
2. Reject missing, extra, unordered, duplicated, or tampered bindings.
3. Preserve research run state append-forward semantics.

Exit criteria:

1. A successful result cannot omit paper evidence.
2. Re-reads verify generation, manifest digest, data checksum, quality report, and
   physical path for every paper artifact.
3. Failure-state output remains invisible to accepted result publication.

### Phase 4 - Release evidence

Deliverables:

1. Local base gate and local Qlib gate.
2. Remote ten-cell unified matrix: the six base cells (macOS/Ubuntu x Python
   3.11/3.12/3.13) plus the four Qlib cells (macOS/Ubuntu x 3.11/3.12) defined by
   `.github/workflows/ci.yml`. Each cell's raw `gate-report.json`,
   `requirements.lock.txt`, `.sha256`, and `environment-cell.txt` artifact is archived
   per cell; the aggregate file only concatenates them and must not restate a cell's
   platform or Python version.
3. Release marker, phase record, evidence index, and immutable historical evidence if
   any defect is reconciled.

Exit criteria:

1. All prior phases exited.
2. Remote matrix passes on the final implementation HEAD and the bound commit equals
   that HEAD (an evidence-only commit may sit on top and is recorded as
   `HEAD_evidence_only`).
3. Remote cell reports are archived under the phase evidence directory.
4. Evidence indexes never record their own digest, and every recorded digest verifies.
5. A documentation-only change on top of an implementation commit is remediated by
   refreshing the evidence index before gate exit.
6. Release evidence is indexed with SHA-256 digests.
7. No acceptance row is `TBD` or unimplemented.

## 4. Acceptance Matrix

`passed` requires an exact persisted test and an evidence path. Planned rows name the
target test and stay `planned` until it exists.

| ID | Phase | Test focus | Test | Status |
|---|---|---|---|---|
| PE1 | 0 | v3 schema/loader validation | `test_v3_request_contract_and_fixtures_validate`, `test_v3_request_rejects_schema_version_2_review_type` | `passed` |
| PE2 | 0 | v3 stage-plan digest sensitivity | `test_v3_stage_plan_digest_is_stable_and_sensitive` | `passed` |
| PE3 | 0 | invalid activation review rejected | `test_v3_activation_review_is_verified_and_sensitive`, `test_v3_request_rejects_v2_stage_plan_digest` | `passed` |
| PE4 | 0 | v3/v4 runtime refused, unknown versions fail closed | `test_v4_and_v3_share_the_stage_order_and_unknown_versions_fail_closed`, `test_v3_and_v4_execution_are_refused_until_the_paper_runtime_lands` | `passed` |
| PE5 | 0R | template v2 and request v5 validate, v1 template rejected | `test_paper_execution_template_v2_and_request_v5_validate`, `test_request_v5_validates` | `passed` |
| PE6 | 0R | request v5 rejects v3/v4 stage digest and review type | `test_request_v5_rejects_superseded_stage_plan` | `passed` |
| PE7 | 0R | state/result widening keeps v1/v3 documents unchanged | `test_state_and_result_widening_is_compatibility_preserving` | `passed` |
| PE8 | 0R | stage-plan golden vectors persisted and stable | `test_research_stage_plan_golden_vectors_are_persisted` | `passed` |
| PE9 | 0R | initial state and opening cash present, closed either/or | `test_template_v2_requires_initial_state_or_bound_state` | `passed` |
| PE10 | 0R | set digest recomputation matches, drift rejected | `test_set_binding_digest_recomputation_and_drift` | `passed` |
| PE11 | 0R | quality helper emits the exact four registry checks | `test_paper_quality_checks_match_registry_allowed_checks` | `passed` |
| PE12 | 0R | negative fixtures: schema rejection and manifest tampering | `test_paper_execution_negative_fixtures_fail_closed` | `passed` |
| PE13 | 0.5 | calendar snapshot published and cross-checked | `test_calendar_snapshot_publish_and_crosscheck` | `passed` |
| PE14 | 0.5 | suspension snapshot primary source is bars `status` | `test_suspension_snapshot_primary_source_is_bars_status` | `passed` |
| PE15 | 0.5 | corporate-action set from dividend partitions | `test_corporate_action_set_digest_recomputes_from_dividend_store` | `passed` |
| PE16 | 0.5 | window before first visible snapshot fails closed | `test_window_before_first_visible_snapshot_fails_closed` | `passed` |
| PE17 | 1 | paper bindings resolve from published stores | `test_paper_bindings_resolve_from_published_stores` | `passed` |
| PE18 | 1 | missing or tampered input fails closed before compute | `test_missing_paper_binding_fails_closed`, `test_tampered_paper_input_fails_closed_before_compute`, `test_v5_without_market_store_fails_closed` | `passed` |
| PE19 | 1 | v1/v2 resolver behaviour unchanged | `test_v1_v2_resolver_behaviour_unchanged` | `passed` |
| PE20 | 2 | one-date initial paper execution end to end | existing paper-execution suite (`test_execution_fills_all_or_none_and_records_fees`) | `passed` |
| PE21 | 2 | multi-date continuation state lineage | existing paper-execution suite (`test_state_builder_links_previous_state`) | `passed` |
| PE22 | 2 | reject reasons preserved: `suspended`, `limit_up`, `limit_down`, `insufficient_cash`, `t1_not_sellable` | existing paper-execution suite (`test_execution_reject_reasons`) | `passed` |
| PE23 | 2 | refused risk decision stops the stage and hides later dates | existing paper-execution suite (risk refusal tests) | `passed` |
| PE24 | 2 | v5 execution enabled and refusal lock replaced | `test_v5_execution_enabled_v3_v4_refused` | `passed` |
| PE25 | 3 | result binds all 4N paper outputs, re-read verified | `test_paper_outputs_reconciled_in_result` | `passed` |
| PE26 | 3 | missing, extra, unordered, duplicated, tampered bindings rejected | `test_paper_result_binding_tampering_rejected` | `passed` |
| PE27 | 3 | failed run publishes no accepted result | `test_paper_failure_hides_accepted_result` | `passed` |
| PE28 | 4 | local gate and Qlib gate on final HEAD | `.gate/gate-report.json` plus the Qlib cell | `planned` |
| PE29 | 4 | remote ten-cell matrix on final HEAD, per-cell reports archived | CI run artifacts under the phase evidence directory | `planned` |
| PE30 | 4 | evidence index freshness and self-hash exclusion | `test_evidence_indexes_are_fresh_and_never_index_themselves` | `planned` |

## 5. Execution Rules

1. Every phase needs local unified gate evidence before exit, plus the ten-cell remote
   matrix for any phase that changes runtime behaviour.
2. Evidence indexes never record their own digest.
3. Any semantic contract change reopens the relevant phase and invalidates later
   evidence; widening an accepted-input enum follows 2.8 and needs the compatibility lock.
4. The runner may only call owning layer stores and engines; it never constructs paper
   artifacts or signs quality reports.
5. No phase may be declared exited while one of its acceptance rows is `planned`.
6. Superseded contract versions (v3/v4) stay published, stay refused at runtime, and
   stay covered by a lock test.

## 6. Open Decisions

1. Whether the paper stage must also honor `index_membership_binding` filtering. v4/v5
   carry it, and Phase 1 resolves it for the factor stage only. Recommendation: reuse
   the same resolved membership sets for the paper universe check, otherwise a target
   weight for a non-member instrument reaches the paper stage after the factor stage
   filtered it out.
2. Whether `price_policy.order_pricing_basis = execution_open_replay` is admitted in the
   first executable slice. Recommendation: allow both, because the paper layer already
   distinguishes them, but require the run report to name the mode.
