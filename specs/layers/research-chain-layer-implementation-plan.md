# Research Chain Layer Implementation Plan

Status: **gated implementation order v0.7; Phase 5 final CR remediation in progress; Phase 6 remains paused until fresh local and remote evidence passes**

Source spec: `specs/layers/research-chain-layer-spec.md`

## 0. Scope

Implement the first full governed research run:

- one reviewed factor slice;
- one reviewed adjusted-return label;
- one reviewed feature schema/dataset;
- one reviewed Qlib export/receipt;
- one Qlib runtime model artifact;
- one reviewed prediction set;
- one top-N equal-weight target portfolio;
- one daily T+1 backtest;
- durable request/state/result manifests and CLI.

The plan does not add algorithms or bypass existing governance.

## 1. Execution Rules

1. `scripts/run_gate.sh` is the mechanical gate.
2. Every phase exit requires focused tests plus a successful unified gate.
3. Contract drafting may occur during plan preparation; runtime reads,
   publications, training, and CLI execution require phase entry criteria.
4. A semantic schema change requires a new schema version.
5. Each phase has a machine-readable record under
   `evidence/research-chain/phase-N/phase-record.json`.
6. Every acceptance ID must map to a concrete test ID or named mechanical
   command before its phase can be declared executable.
7. Evidence cannot be copied from an older commit without preserving the bound
   commit, lockfile digest, and requirements digest.

## 2. Phase 0 — Run Contract Gate

Goal: freeze the run ledger before any orchestration runtime is enabled.

Deliverables:

1. `config/schemas/contracts/research_run_request.v1.json`
2. `config/schemas/contracts/research_run_state.v1.json`
3. `config/schemas/contracts/research_run_result.v1.json`
4. `config/schemas/contracts/model_definition_template.v1.json`
5. `config/schemas/contracts/portfolio_definition_template.v1.json`
6. `config/schemas/contracts/quality_decision.v1.json` as a strict wrapper
   around existing `model_quality_report.v2`, not a replacement report format.
7. Typed loader registration and canonical identity helpers for the three run
   families plus the two definition templates and quality decision wrapper. The
   helpers must be additive to `ModelContractLoader` and expose explicit
   excluded-field sets per family; they must not fork identity rules.
8. Valid fixtures plus at least two negative fixtures for each required family.
9. Golden vectors for request stability under key reorder/run metadata change
   and result instability under output-generation change.
10. Frozen stage enum, stage order, closed `stage_output_binding` schema,
    failure taxonomy, canonical stage-plan payload, quality decision wrapper
    binding enum, provider interface, trust-anchor/checksum rules, and provider
    config path safety.
11. Owning-layer additive read/binding API contracts: factor manifest and
    partition readers, feature-schema/label/universe manifest and data readers,
    `PortfolioDefinitionBinding.bind`, and explicit reuse/readback boundaries
    for existing model, prediction, target-weight, and backtest result APIs.
    These are contracts only; owning runtime implementations remain in their
    later phase slices.
12. Physical layout contract:
   - `research_runs/requests/request=<request_content_generation_id>/run=<run_id>/manifest.json`;
   - `research_runs/states/request=<request_content_generation_id>/run=<run_id>/stage=<NN>/manifest.json`;
   - `research_runs/results/request=<request_content_generation_id>/run=<run_id>/result=<result_content_generation_id>/manifest.json`;
   - staging and quarantine directories are outside accepted state/result paths.
13. Code fingerprint and environment-profile rules.

Entry criteria: none.

Exit criteria:

1. All six schemas validate representative fixtures and negative fixtures.
2. Every negative fixture produces a typed rejection.
3. Request content identity is stable under run metadata and key reorder; the
   complete manifest digest changes under run metadata.
4. Result identity changes for any bound output, stage status, runner identity,
   or semantic request field change and remains stable when only run metadata
   changes.
5. State and result paths reject traversal, symlink escape, missing parent, and
   overwrite; provider configuration references reject the same path failures.
6. Golden vectors are persisted and fail closed when absent.
7. Provider binding, checksum, trust-anchor, missing/rejected, and malformed
   signature paths are tested.
8. The Phase 0 acceptance rows replace `planned:` IDs with concrete test IDs,
   fixture files, and evidence paths before exit.
9. Unified gate is green and evidence is preserved.

Unblocks the dry-run resolver only.

## 3. Phase 1 — Resolver and Dry Run

Goal: make request resolution mechanical before any publication.

Deliverables:

1. `ResearchChainRequestResolver` validating the request and all upstream
   manifest references.
2. Typed error mapping for all spec §10 failure reasons.
3. Ordered execution-plan digest with this exact canonical payload:
   `{"schema_version":"v1","request_content_generation_id":...,"stage_plan_sha256":...,"stage_bindings":[{"stage":...,"output_family":...,"generation_id":...,"manifest_digest_sha256":...,"data_checksum_sha256":...}]}`
   for resolved upstream/produced bindings, in stage order. This is
   `resolved_execution_plan_sha256`, distinct from the reviewed request-level
   `stage_plan_sha256`. State/result bindings record the request manifest digest
   for audit, but result identity excludes it as specified in the source spec.
4. Dry-run state snapshot with `intent=dry_run`.
5. No factor, dataset, model, prediction, portfolio, or backtest mutation.
6. Negative tests for missing, tampered, malformed, duplicate, unordered,
   wrong-generation, wrong-digest, provider-unreachable, provider-config-invalid,
   untrusted-key, and unsupported owning-family bindings.

Entry criteria:

- Phase 0 exits.

Exit criteria:

1. Dry run resolves the representative valid request.
2. Every negative request path fails with the expected typed reason.
3. Dry-run state has no output generation IDs.
4. Repeated dry runs produce the same execution-plan digest when inputs are
   unchanged.
5. Unified gate remains green.

Unblocks stage adapters.

## 4. Phase 2 — Factor and Dataset Stage Adapters

Goal: connect the existing governed factor and model dataset APIs.

Deliverables:

1. Factor adapter binding a governed already-published factor partition through
   the owning `FactorStore.read_manifest/read_partition` contracts only.
   `FactorEngine` execution remains outside this adapter; a dedicated engine
   adapter is a separate later slice and must not be added as a bypass.
2. Dataset adapter invoking `LabelBuilder`, `FeatureSchemaBuilder`, owning
   feature-schema/label/universe reader contracts, `DatasetBuilder`, and
   `DatasetWriter` only.
3. Stage state writers capturing exact generation/digest/path bindings.
4. Missing/tampered/misbound upstream rejection tests for canonical bars,
   adjusted bars, universe snapshot, factor definition, label definition, and
   quality decisions.
5. Readback validation after every publication.
6. Explicit handling of already-published exact generations: bind only after
   manifest digest and data checksum match; otherwise fail.

Entry criteria:

- Phase 1 exits.
- Factor, preprocessing, and model dataset APIs are released.

Exit criteria:

1. Full factor/dataset stage succeeds from governed inputs.
2. Every stage output is read back through its owning API.
3. Failed or quarantined outputs cannot enter stage state.
4. Semantic input changes change downstream manifest identities.
5. Unified gate remains green.

Unblocks Qlib export/training.

## 5. Phase 3 — Qlib Training and Prediction Adapters

Goal: train and predict without bypassing the model-layer boundary.

Deliverables:

1. Before model-stage runtime, replace the factor stage's checksum-only
   quality-decision input with typed reviewed-decision validation covering
   schema, signature, binding type, subject generation, and checksum.
2. Qlib export and receipt adapter invoking `QlibDatasetExporter`,
   `QlibDatasetExporter.read`, and `QlibInitReceiptBuilder`.
3. Model run/artifact adapter invoking `ModelRunBuilder`,
   `QlibRuntimeTrainer`, and `ArtifactStore`.
4. Prediction adapter invoking `PredictionBuilder.build` and
   `PredictionBuilder.read`.
5. Stage state bindings for dataset, export, receipt, run content, definition,
   artifact, and prediction generations.
6. Tests for export tampering, receipt mismatch, unverified export,
   artifact tampering, prediction key/score/eligibility failure, and quality
   report rejection.

Entry criteria:

- Phase 2 exits.
- Qlib runtime trainer and adapter dependencies are available in the declared
  gate environment.

Exit criteria:

1. Training consumes only the verified governed Qlib export.
2. Artifact and prediction readback succeeds.
3. A tampered export, receipt, artifact, prediction manifest, or quality report
   fails before downstream stage execution.
4. Repeated locked-environment training satisfies the declared reproducibility
   mode.
5. Unified gate with Qlib extras remains green.

Unblocks downstream portfolio/backtest stages.

## 6. Phase 4 — Portfolio and Backtest Adapters

Goal: consume governed predictions without manual DataFrame wiring.

Deliverables:

1. Portfolio adapter invoking `PortfolioBuilder` and `TargetWeightStore`.
2. Backtest adapter invoking `BacktestEngine` and `BacktestResultStore`.
3. Price/calendar/suspension/corporate-action input resolution from governed
   manifests.
4. Ordered target-weight binding list matching backtest decision dates.
5. Tests for missing prediction, tampered prediction, universe mismatch, weight
   overwrite, backtest price tampering, suspension/corporate-action rejection,
   and missing quality decision.
6. Result readback for equity curve, daily metrics, and fills.

Entry criteria:

- Phase 3 exits.

Exit criteria:

1. Prediction generation is the only score source for portfolio construction.
2. Target weights bind prediction, universe, and portfolio definition.
3. Backtest result binds ordered target weights and price/calendar/suspension/
   corporate-action sources.
4. Tampered or missing inputs fail closed.
5. Unified gate remains green.

Unblocks final reconciliation.

## 7. Phase 5 — Runner, CLI, and End-to-End Gate

Goal: expose one governed command and prove the full chain.

Deliverables:

1. `ResearchChainRunner` implementing the fixed stage graph.
2. State/result publishers implementing the Phase 0 store contracts with
   atomic, manifest-last promotion.
3. `uq-research-run` CLI with `--project-root`, `--data-root`, and
   `--mode dry-run|execute`.
4. JSON exit/status contract.
5. End-to-end test from governed inputs to published backtest result.
6. End-to-end negative tests for a failure at every stage.
7. Deterministic rebuild test under the locked environment.
8. Evidence-index test ensuring all output identities are present.

Entry criteria:

- Phase 4 exits.

Exit criteria:

1. Dry run publishes only request and state, never downstream artifacts.
2. Execute publishes all expected outputs and one successful result.
3. Each simulated stage failure stops later stages.
4. The final result can be revalidated from disk.
5. Rebuild under one locked environment satisfies the declared reproducibility
   mode.
6. Unified gate remains green.

Unblocks release reconciliation.

## 8. Phase 6 — Release Reconciliation

Deliverables:

1. Final local gate evidence bound to implementation HEAD.
2. Remote 10-cell unified-gate matrix result.
3. Updated spec/plan status markers.
4. Release record and evidence index.
5. Machine-readable Phase 0–6 records.

Entry criteria:

- Phase 5 exits.

Exit criteria:

1. Local gate passes on final implementation commit.
2. Remote 10-cell matrix passes.
3. All acceptance rows are executable or explicitly deferred with release-scope
   limitation.
4. Security/lineage/fail-closed rows cannot be deferred.
5. Release record is committed.

## 9. Acceptance Matrix Expansion

Rows marked `planned:` do not unlock runtime. They must be replaced by existing
test node IDs before their owning phase exits.

| ID | Phase | Test ID | Fixture path | Evidence path | Status |
|---|---|---|---|---|---|
| RC0a | 0 | `tests/test_research_chain_phase0.py::test_valid_and_negative_fixtures_are_persisted` | `evidence/research-chain/phase-0/fixtures/` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0b | 0 | `tests/test_research_chain_phase0.py::test_valid_and_negative_fixtures_are_persisted` | `evidence/research-chain/phase-0/fixtures/` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0c | 0 | `tests/test_research_chain_phase0.py::test_normative_stage_order_is_enforced` | `evidence/research-chain/phase-0/fixtures/` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0d | 0 | `tests/test_research_chain_phase0.py::test_result_identity_is_sensitive_to_governed_content` | `evidence/research-chain/phase-0/golden-vectors/identity-golden-vectors.json` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0e | 0 | `tests/test_research_chain_phase0.py::test_research_layout_rejects_traversal_missing_parent_and_overwrite` | `src/uq/research_chain/contracts.py` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0f | 0 | `tests/test_research_chain_phase0.py::test_owning_layer_read_boundaries` | `src/uq/research_chain/owning_contracts.py` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0g | 0 | `tests/test_research_chain_phase0.py::test_factor_store_read_manifest_boundary` | `src/uq/factors/store.py` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0h | 0 | `tests/test_research_chain_phase0.py::test_universe_snapshot_store_read_boundaries` | `src/uq/contracts/artifacts.py` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0i | 0 | `tests/test_research_chain_phase0.py::test_portfolio_definition_binding` | `src/uq/portfolio/builder.py` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0j | 0 | `tests/test_research_chain_phase0.py::test_stage_ledger_rejects_gaps_and_terminal_regression` | `evidence/research-chain/phase-0/fixtures/` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0k | 0 | `tests/test_research_chain_phase0.py::test_stage_ledger_rejects_failed_without_reason_and_later_progress` | `evidence/research-chain/phase-0/fixtures/` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0l | 0 | `tests/test_research_chain_phase0.py::test_provider_config_rejects_unregistered_reference_and_binding` | `src/uq/research_chain/contracts.py` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC0m | 0 | `tests/test_research_chain_phase0.py::test_research_layout_resolves_relative_paths_under_data_root` | `src/uq/research_chain/contracts.py` | `evidence/research-chain/phase-0/gate-reports/gate-report.json` | passed |
| RC1a | 1 | `tests/test_research_chain_phase1.py::test_request_resolves_all_upstreams_and_is_deterministic` | `evidence/research-chain/phase-0/fixtures/research_run_request-valid.json` | `evidence/research-chain/phase-1/gate-reports/gate-report.json` | passed |
| RC1b | 1 | `tests/test_research_chain_phase1.py::test_dry_run_state_contains_no_downstream_outputs` | `evidence/research-chain/phase-1/phase-record.json` | `evidence/research-chain/phase-1/gate-reports/gate-report.json` | passed |
| RC1c | 1 | `tests/test_research_chain_phase1.py::test_provider_failures_are_typed` + `tests/test_research_chain_phase1.py::test_request_failures_are_typed` | `tests/test_research_chain_phase1.py` | `evidence/research-chain/phase-1/gate-reports/gate-report.json` | passed |
| RC2a | 2 | `tests/test_research_chain_phase2.py::test_factor_stage_binds_verified_partition` | `tests/test_research_chain_phase2.py` | `evidence/research-chain/phase-2/final-gate-reports/gate-report.json` | passed |
| RC2b | 2 | `tests/test_research_chain_phase2.py::test_dataset_stage_binds_reviewed_policy_and_verified_inputs` | `tests/test_research_chain_phase2.py` | `evidence/research-chain/phase-2/final-gate-reports/gate-report.json` | passed |
| RC2c | 2 | `tests/test_research_chain_phase2.py::test_dataset_stage_rejects_wrong_reviewed_quality_decision` + `tests/test_research_chain_phase2.py::test_dataset_stage_rejects_preprocessing_input_mismatch` | `tests/test_research_chain_phase2.py` | `evidence/research-chain/phase-2/final-gate-reports/gate-report.json` | passed |
| RC3a | 3 | `tests/test_research_chain_phase3.py::test_model_chain_exports_trains_and_predicts` | `tests/test_research_chain_phase3.py` | `evidence/research-chain/phase-3/final-gate-reports/gate-report.json` | passed |
| RC3b | 3 | `tests/test_research_chain_phase3.py::test_tampered_export_rejects_before_model_stage` + `tests/test_research_chain_phase3.py::test_tampered_artifact_rejects_read` | `tests/test_research_chain_phase3.py` | `evidence/research-chain/phase-3/final-gate-reports/gate-report.json` | passed |
| RC3c | 3 | `tests/test_research_chain_phase2.py::test_factor_stage_rejects_wrong_reviewed_decision` + `tests/test_research_chain_phase3.py::test_wrong_artifact_quality_report_rejects_publication` | `tests/test_research_chain_phase2.py` | `evidence/research-chain/phase-3/final-gate-reports/gate-report.json` | passed |
| RC3d | 3 | `github-actions:qlib-runtime unified gate` | `evidence/research-chain/phase-3/final-head-ci/33945900595/artifacts/qlib-gate-report-*` | `evidence/research-chain/phase-3/final-head-ci/33945900595/run.json` | passed |
| RC4a | 4 | `tests/test_research_chain_phase4.py::test_portfolio_stage_binds_prediction_and_universe` + `tests/test_research_chain_phase4.py::test_portfolio_chain_links_previous_target_generation` | `evidence/research-chain/phase-4/` | `evidence/research-chain/phase-4/implementation-gate-reports/gate-report.json` | passed |
| RC4b | 4 | `tests/test_research_chain_phase4.py::test_backtest_stage_publishes_ordered_weight_lineage` | `evidence/research-chain/phase-4/` | `evidence/research-chain/phase-4/implementation-gate-reports/gate-report.json` | passed |
| RC4c | 4 | `tests/test_research_chain_phase4.py::test_portfolio_prediction_binding_mismatch_fails` + `tests/test_research_chain_phase4.py::test_portfolio_weight_overwrite_fails` + `tests/test_research_chain_phase4.py::test_wrong_portfolio_quality_decision_fails` + `tests/test_research_chain_phase4.py::test_backtest_rejects_corporate_action_overlap` + `tests/test_research_chain_phase4.py::test_backtest_rejects_tampered_price_panel` | `evidence/research-chain/phase-4/` | `evidence/research-chain/phase-4/final-head-ci/33951272818/aggregated-gates.json` | passed |
| RC5a | 5 | `tests/test_research_chain_phase5.py::test_full_research_chain_end_to_end` | `evidence/research-chain/phase-0/fixtures/research_run_request-valid.json` | `evidence/research-chain/phase-5/implementation-gate-reports/gate-report.json` | passed |
| RC5b | 5 | `tests/test_research_chain_phase5.py::test_failed_stage_stops_run_and_publishes_failure` + `tests/test_research_chain_phase5.py::test_every_stage_failure_stops_later_stages` | `tests/test_research_chain_phase5.py` | `evidence/research-chain/phase-5/implementation-gate-reports/gate-report.json` | passed |
| RC5c | 5 | `tests/test_research_chain_phase5.py::test_cli_dry_run_publishes_only_request_and_state` + `tests/test_research_chain_phase5.py::test_cli_execute_requires_external_decisions` | `tests/test_research_chain_phase5.py` | pending final remediation gate | blocked |
| RC5d | 5 | `tests/test_research_chain_phase5.py::test_locked_environment_rebuild_is_reproducible` | `evidence/research-chain/phase-5/implementation-gate-reports/requirements.lock.txt` | `evidence/research-chain/phase-5/implementation-gate-reports/gate-report.json` | passed |
| RC5e | 5 | `scripts/run_gate.sh` | `evidence/research-chain/phase-5/remediation-gate-reports/gate-report.json` | remediation HEAD report | pending |
| RC5f | 5 | `github-actions:10-cell unified gate` + `scripts/verify_research_evidence.py` | `evidence/research-chain/phase-5/final-head-ci/<remediation-run-id>/aggregated-gates.json` | `evidence/research-chain/phase-5/final-head-ci/<remediation-run-id>/run.json` | blocked |
| RC5g | 5 | `tests/test_research_chain_phase5.py::test_full_research_chain_end_to_end` | `src/uq/research_chain/runner.py` | final gate | pending |
| RC5h | 5 | `tests/test_research_chain_phase5.py::test_full_research_chain_end_to_end` | `src/uq/models/qlib_export.py` | final gate | pending |
| RC5i | 5 | `scripts/verify_research_evidence.py` + `tests/test_research_chain_phase5.py::test_remote_evidence_aggregation_is_mechanically_verified` | `scripts/verify_research_evidence.py` | `evidence/research-chain/phase-5/final-head-ci/33960583993/` | passed |
| RC6a | 6 | `scripts/run_gate.sh` | `evidence/research-chain/release/final-gate-report.json` | `evidence/research-chain/release/final-gate-report.json` | blocked |
| RC6b | 6 | `github-actions:10-cell unified gate` | `evidence/research-chain/release/remote-matrix/` | `evidence/research-chain/release/remote-matrix/` | blocked |

## 10. Risks

| Risk | Gate | Resolution |
|---|---|---|
| Quality decisions may be bound to provisional generations | Blocks production use | Freeze the v2 wrapper and owning binding semantics in Phase 0; no runner may call decision creation directly |
| Stage adapters may duplicate layer logic | Blocks implementation | Adapters may translate and validate only; computation/publication stays in owning stores |
| Partial failure leaves immutable downstream outputs | Cannot be undone | Keep outputs immutable, mark run failed, exclude them from successful result |
| Cross-platform bytes differ | Cannot be solved by runner | Compare logical fingerprints cross-platform; byte checks only in locked cells |
| Factor layer provenance remains pending | Blocks factor release claim | Research Chain may exercise governed slices but cannot certify the unresolved official reference prices |

## 11. Immediate Next Actions

1. Complete Phase 5 CLI provider wiring or explicitly move execute-mode runtime to Phase 6 without leaving the current stub overclaimed.
2. Preserve fresh local gate evidence at the remediation HEAD.
3. Trigger remote CI, preserve the ten-cell raw artifacts/aggregation, and run the mechanical verifier.
4. Only after Phase 5 re-exit, perform Phase 6 release reconciliation.
