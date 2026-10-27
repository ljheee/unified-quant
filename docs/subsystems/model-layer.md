# Model Layer (Labels, Features, Dataset, Qlib, Training, Prediction)

> Current behavior reference. Last checked: `2026-10-20` against HEAD `54676a0a3b377d1b88da09309d96d6a7c6535419`. Based on source code, configuration, tests, and release evidence; unverified items are marked `UNKNOWN`.

## Responsibilities and Boundaries

This subsystem turns governed factor partitions into published labels, feature schemas, stateless preprocessing, model datasets, Qlib exports and receipts, trained model runs/artifacts, and prediction sets. It owns the *governance* of every one of those families: identity, external quality-decision binding, immutability, and read-back reconciliation.

It does **not** compute factors (`docs/subsystems/factor-layer-and-qlib-adapter.md`) and does not orchestrate (`docs/subsystems/research-chain.md`). Every published family requires an externally signed, reviewed quality decision; a publisher may never self-approve (`evidence/research-chain/release/release-record.json` `external_quality_reviews.publisher_self_approval_forbidden`).

## Authoritative Sources

| Fact | Authoritative path |
| --- | --- |
| Contract registry, identity rules, quality-decision binding | `src/uq/contracts/model_layer.py:135` `model_manifest_identities`, `:975` `bind_reviewed_quality_decision`, `:1043` `resolve_subject_quality_decision`, `:802` `ModelQualityReviewRegistry` |
| Label construction and horizon semantics | `src/uq/models/labels.py:66` `LabelBuilder`, `:53` `_formula_sha256` |
| Label publication | `src/uq/models/label_store_writer.py:47` `LabelSetWriter` |
| Feature schema | `src/uq/models/features.py:61` `FeatureSchemaValidator` |
| Stateless cross-sectional preprocessing and the row-drop rule | `src/uq/models/feature_preprocessing.py:22` `FeaturePreprocessorBuilder`, `:223` `complete_rows` |
| Dataset split validation (purge/embargo) | `src/uq/models/dataset.py:14` `SplitValidator` |
| Dataset publication | `src/uq/models/dataset_writer.py:28` `DatasetWriter` |
| Qlib export and init receipt | `src/uq/models/qlib_export.py:25` `QlibDatasetExporter`, `:264` `QlibInitReceiptBuilder` |
| Deterministic trainer and artifact store | `src/uq/models/trainer.py:20` `ModelTrainer`, `:102` `ArtifactStore` |
| True Qlib runtime trainer | `src/uq/models/qlib_runtime.py:30` `QlibRuntimeTrainer` |
| Accepted-factor index (read side) | `src/uq/models/accepted_store.py:14` `AcceptedFactorIndexRuntime` |

Contract schemas live in `config/schemas/contracts/*.json` (70 files); manifests for canonical-v2 and factor manifest v1/v2 live in `config/schemas/manifests/`.

## Primary Runtime Paths

Producers exist in two forms: owning-layer APIs usable directly, and scripts for the families the research chain binds.

1. **Labels.** `LabelBuilder` computes a 5-trading-day adjusted-close label over the *unfiltered* per-instrument series; eligibility (suspension, limit, listing age, delisting) is applied afterwards, so the horizon counts trading rows, not surviving rows. That ordering is pinned into the manifest by `_formula_sha256`, which includes the row basis in the formula identity. `LabelSetWriter` is the production publisher (`scripts/build_label_sets.py`), defaulting to per-instrument sets and supporting `--combined` for the single set the research chain binds.
2. **Feature schema and preprocessing.** `FeaturePreprocessorBuilder` builds one transform per manifest (`standardize_cross_section` or `rank_cross_section`), stateless and same-date cross-sectional. `complete_rows` is the single definition of "trainable row": rows with any undefined feature are dropped **before** the transform so the cross-sectional statistics describe exactly the rows that will be trained on, and so the published `input_frame_sha256` describes the frame actually handed over. A null **label** is a separate policy from a null **feature** (`drop_unlabeled_rows` vs `drop_incomplete_rows`). For v6, label-aware row selection is published first as `sample_construction.v1` (`src/uq/models/sample_construction.py`, producer `scripts/publish_sample_construction.py`); preprocessing consumes that selected frame and never reads the label.
3. **Dataset.** `DatasetWriter` requires an externally reviewed quality report, validates the feature schema against the frame, validates splits, serializes, re-reads and reconciles. `SplitValidator` evaluates purge/embargo in **time order**, so a `[validation, train]` declaration is not mistaken for a leak.
4. **Qlib export/receipt.** `QlibDatasetExporter` produces a Qlib-initializable layout and an init receipt; it does not import or install Qlib. Identity fields (`generation_id`, `manifest_digest_sha256`) are finalized **before** the receipt is written to disk, because only a resume path reads the persisted receipt — which is exactly how an earlier defect stayed invisible.
5. **Training.** `ModelTrainer` requires a reviewed definition, an explicitly declared `train_period` (fitting on the full frame would leak validation/test labels), and rejects non-finite features or labels before solving, so a NaN model can never be published with a legitimate checksum. `QlibRuntimeTrainer` trains through the Qlib model API against a verified governed export. The artifact's `runtime_version` is the authoritative Qlib version for inference; the research chain passes it to the runtime guard so a model trained under one Qlib version is never scored under another.
6. **Prediction.** `PredictionBuilder` publishes prediction sets from externally supplied scores; the research chain does not yet auto-load an artifact to infer today's scores (`docs/layer-architecture.md` §2).

## Multiple Implementations / Entry Points

- **Two trainers.** `ModelTrainer` (deterministic NumPy ridge, `regularized_linear` only) and `QlibRuntimeTrainer` (real Qlib model API). The Qlib one is only exercised where the Qlib extra is installed; in `.venv` its tests skip (28 of 1265), and in `.venv-qlib` they run (0 skips, 1293 passed).
- **Two label publication modes** — per-instrument (default) and `--combined` (one set covering all instruments). The research chain binds exactly one set, so an execute run needs `--combined`.
- **Producers vs. read-only families.** For a v5 execute, publishers ship for factors, universe snapshots, label sets and feature preprocessing; `adjusted_price` and `backtest_config` have write-side APIs in `src/uq/research_chain/owning_contracts.py` (`AdjustedPriceDatasetStore` at `:89`, `BacktestConfigStore` at `:358`) but only test helpers call them today (`docs/user-guide.md` §7).
- **Deprecated exporter.** `src/uq/exporters/qlib.py` is outside the accepted path; `src/uq/models/qlib_export.py` is the governed one.

## State and Data Contracts

Each family is content-addressed and immutable: `<family>/generation=<id>/{manifest.json,data.parquet}` or the family-specific layout, with `generation_id` recomputed over the manifest minus itself and a manifest digest bound to the same payload. Publication takes a lock, writes a staging directory, reads the artifact back, and reconciles dtype/column/row/fingerprint against what the manifest declares before atomically renaming. An existing partition raises a conflict rather than being overwritten.

Quality decisions are Ed25519-signed and bound to a subject generation; `bind_reviewed_quality_decision` refuses a decision that is not reviewed, not bound to this subject, or signed by a repository test-mode key while `UQ_RUNTIME_MODE=production` (`src/uq/runtime.py:34`).

## Qlib export identity and read-time verification

Two facts about `QlibDatasetExporter` that are not visible from a call site:

1. **The identity is content-derived, not passed in.** ``export`` derives
   ``export_generation_id`` by hashing the files it produced (including each file's
   checksum and byte size), so the id cannot be known before the files exist. History:
   the original implementation used the caller-supplied ``generation_id``; the change
   that bound quality reports to the export's real identity moved derivation inside
   the function. The consequence is that a caller holding only the *dataset* generation
   id cannot locate the export — the two ids differ, and one is not derivable from the
   other without staging the same bytes.
2. **``provider_uri_sha256`` and ``export_layout`` sit outside the identity.** A URI
   says where the artifact will be read from, not what it contains, so two runs with
   different URIs produce byte-identical exports with the same id. That is why a
   content comparison cannot detect a cross-URI reuse, and ``read()`` accepts an
   optional ``provider_uri`` and refuses a mismatch. The check is read-time because at
   write time there is no "correct" URI to compare against.

``exporter_fingerprint`` is *inside* the identity, so a mismatch is already refused as
a generation mismatch — do not add a second check for it.

**A retry of the same request and run does not republish.** ``ResearchChainRunner``
resumes from the stage ledger (``_resume_from_passed_stages``): a stage recorded as passed is
skipped, so the writer is never reached twice for one run. There is deliberately no
"already exists" recovery at the call site -- the refusal is the correct answer when a
*different* run reaches a writer with content whose identity it has already published,
because the identity is a content hash and does not encode the run.

## Constraints, Failures, and Compatibility

- Null handling is policy, not accident: a null label means "insufficient future rows" and is dropped before finiteness checks; a null feature makes a row unusable for learning but is preserved for inference. Folding the two into one policy either hides defects or deletes valid rows.
- Training on a frame that spans all splits is refused; `train_period` is mandatory.
- Qlib runtime processors are disabled unless they consume a governed preprocessing manifest (`specs/architecture.md` §11.4).
- A receipt persisted before its identity is finalized is a permanent on-disk defect; the write order is now identity-first and locked by `tests/test_model_qlib_export.py:173` `test_persisted_receipt_carries_its_final_identity`.
- Semantic or unit changes require a new version (`AGENTS.md`); the adjustment-snapshot history at `src/uq/contracts/adjustment_snapshot.py:25` records why.

## Verification Evidence

- `.venv/bin/python -m pytest` → `1191 passed, 25 skipped`; `.venv-qlib/bin/python -m pytest` → `1216 passed, 0 skipped`. Counts updated by the change that adds the read-time provider-URI verification section above.
- Model-layer suites: `tests/test_model_layer_contracts.py`, `tests/test_model_labels_dataset.py`, `tests/test_model_features_dataset_writer.py`, `tests/test_feature_preprocessing.py`, `tests/test_model_trainer.py`, `tests/test_model_qlib_export.py`, `tests/test_model_predictions.py`, `tests/test_model_end_to_end.py` (partly Qlib-gated), `tests/test_build_label_sets.py`, `tests/test_label_set_writer.py`, `tests/test_drop_unlabeled_rows.py`, `tests/test_accepted_store_runtime.py`, `tests/test_model_store_isolation.py`, `tests/test_release_acceptance.py`.
- Acceptance matrix: `evidence/model-layer/acceptance-matrix.json` records 59 requirements, 59 passed, 0 blocked, each citing an exact `test_id`.
- Release binding: `evidence/research-chain/release/release-record.json` binds v0.8 to `release_head` `9f208267e585b0f95a219af2dd3f9367a5fb4d3e`, CI run `34004669663`, 10 covered environment cells, and declares the factor-layer reference-price provenance limitation.
- Quality-decision trust anchors: `config/model-quality-trust-anchor.v1.json`, `config/risk-review-trust-anchor.v1.json`, `config/research-stage-plan-review-anchor.v1.json`, `config/research-stage-plan-review-anchor.v3.json`, `config/research-stage-plan-review-anchor.v4.json`, `config/research-stage-plan-review-anchor.v5.json`.

## Unknowns and Pending Confirmation

- `UNKNOWN`: a Signal/Inference layer that loads a published artifact and produces daily scores automatically does not exist; prediction scores are still supplied externally as `scores-json` (`docs/layer-architecture.md` §5).
- `UNKNOWN`: coverage of `QlibRuntimeTrainer` rejection paths (single train split, export calendar not covering the window) in an environment without Qlib — those paths only execute under the Qlib extra, so `.venv` green is not evidence for them.
