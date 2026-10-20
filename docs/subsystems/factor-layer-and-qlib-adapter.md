# Factor Layer and Qlib Adapter

> Current behavior reference. Last checked: `2026-10-20` against HEAD `54676a0a3b377d1b88da09309d96d6a7c6535419`. Based on source code, configuration, tests, and previous review records; unverified items are marked `UNKNOWN`.

## Responsibilities and Boundaries

This subsystem computes governed factors from canonical partitions and publishes them as immutable factor partitions, plus the reviewed Alpha158 Qlib computation path. It owns the factor-set registry semantics, per-factor computation, the factor partition manifest identity, and the logical fingerprint used to detect data drift on read.

It does **not** own the canonical partitions it reads (`docs/subsystems/ingest-and-canonical-store.md`), the adjusted-price line it can bind (`docs/subsystems/adjustment-and-corporate-actions.md`), labels and feature schemas (`docs/subsystems/model-layer.md`), or the orchestration that calls it (`docs/subsystems/research-chain.md`). Qlib is a derived consumer here, never a bypass around governance.

## Authoritative Sources

| Fact | Authoritative path |
| --- | --- |
| Reviewed factor-set definitions | `config/factor-sets/*.json`, loaded by `src/uq/contracts/factor_governance.py:31` `FactorRegistry` |
| Governed publish path and identity | `src/uq/factors/store.py:28` `FactorStore.publish`, `src/uq/factors/store.py:259` `factor_generation`, `src/uq/contracts/gate_contracts.py:94` `factor_manifest_identities` |
| Governed read path | `src/uq/factors/store.py:563` `read_factor_partition`, `src/uq/factors/store.py:516` `_read_verified_factor_manifest` |
| Raw-price computation and fingerprint versions | `src/uq/factors/raw_price.py:16` `calculate_raw_price_factors`, `:56` `logical_fingerprint` |
| Adjusted-price factor computation | `src/uq/factors/adjusted_price.py:46` `calculate_adjusted_factors` |
| Planning/execution facade and PIT cutoffs | `src/uq/factors/engine.py:106` `FactorEngine` |
| Qlib Alpha158 adapter (isolated subprocess) | `src/uq/factors/qlib_adapter.py:98` `QlibFactorAdapter`, worker at `:63` `_qlib_worker` |
| Partition-set binding for multi-partition requests | `src/uq/factors/partition_set.py:47` `build_factor_partition_set` |
| Accepted-factor index (model-layer read side) | `src/uq/models/accepted_store.py:14` `AcceptedFactorIndexRuntime` |

## Primary Runtime Paths

Production producer is `scripts/publish_factor_partitions.py` (canonical bars → projection → compute → publish, emitting a `factor_partition_set` binding). In-process entry is `FactorEngine.compute` → `build_request` → `plan` → `create_result`, with the registry injected into the store rather than rebuilt from the source tree (a prior defect: publish-time and read-time registries could diverge).

`FactorStore.publish` enforces, in order:

1. the factor set is not contract-only (`alpha360` has zero factors and cannot be published) and the definition is registered;
2. the input binding is reviewed — either the definition's own `input_bindings` (v2/v3 sets) or the historical name-based rules for `basic`/`adjusted`;
3. raw-price factors must **not** carry adjustment lineage, and adjusted factors must carry it (`adjustment_snapshot_id` + `effective_date_table_checksum`);
4. the frame passes `_validate_factor_frame`;
5. generation derived from the manifest equals the independently computed generation;
6. a publication lock is taken, the partition must not already exist, the staging directory is written and the Parquet re-read, then `_reconcile_output` compares the restored frame against the in-memory frame by logical fingerprint.

Reads (`read_factor_partition`) re-validate the manifest against its declared `factor_manifest.v{n}.json` schema, recompute identities, and verify the logical fingerprint of the restored frame. Fingerprint versions 1–3 are all accepted on read; versions 2 and 3 fixed real collisions (`NaN` vs `-0.0`, and `+inf` vs `-inf` sharing a null mask), and reads were kept compatible with older partitions rather than being cut off.

Qlib computation runs in a **subprocess** (`_qlib_worker`): the parent validates the payload — instrument grammar, allowed expression field whitelist — and the child writes the result, preserving canonical `datetime64` dtype and uppercasing instruments. A pyqlib interpreter that is absent is a typed `QlibNotInstalledError`, and the tests that require it are skipped rather than failing silently.

## Multiple Implementations / Entry Points

- **Alpha158 is implemented; Alpha360 is contract-only.** `config/factor-sets/alpha360-v1.json` has zero factors, so publishing it is refused. Any document claiming Alpha360 support is describing a reserved contract, not an implementation.
- **Two Alpha158 versions** exist (`1.0.0` bound to `bars_daily/research-v1`, `2.0.0` bound to `bars_daily/v2` via `input_bindings`). v2 inherits all 157 expressions and fingerprints verbatim from v1; only the input line and `reviewed_engine_versions` differ. `required_columns` still says `datetime` in both, because the input-line rename is handled by the projection in `src/uq/factors/inputs.py`.
- **Legacy exporter is deprecated.** `src/uq/exporters/qlib.py` is explicitly outside the model-layer accepted publication path and must not be used for training or prediction; governed Qlib export lives at `src/uq/models/qlib_export.py`.
- **Registry injection points.** `FactorStore` and `AcceptedFactorIndexRuntime` both accept an injected `FactorRegistry`; both previously rebuilt one from the module path, which allowed publish/accept disagreement.

## State and Data Contracts

Layout is `$ROOT/factors/dataset=…/schema_version=…/factor_set=…/factor_version=…/date=YYYY-MM-DD/{data.parquet,manifest.json}`. The manifest carries upstream generation and data checksums, the quality-report checksum, adjustment lineage when applicable, a code fingerprint, decision time and run-visible cutoff in `Asia/Shanghai`, and a serialization profile (`parquet-v1`: snappy, dictionary enabled, row group 64 000) so identical inputs reproduce byte-identical artifacts on a certified environment cell.

Factor partitions are immutable. Republishing the same generation is a conflict, not an overwrite.

## Constraints, Failures, and Compatibility

- A wrong input dataset/schema binding fails closed with a message naming the reviewed binding, so a caller can distinguish a wrong argument from a governance gap.
- `close_location_1d` is undefined when `high == low` (a one-word limit board); the code returns NaN rather than a fabricated 0. A measured review found 47 such names in a 21-session A-share window, **all** explained by limit-up/limit-down boards, with no genuine flat non-limit case — so treating that null as "missing data" and deleting the instrument would penalize a legal market state (`docs/universe-coverage.md`).
- `amount_20d` / `volume_ratio_20d` are genuinely absent during the 20-session warm-up; that is a lead-in effect, not a defect.
- The logical fingerprint is a *tolerance* comparison, not byte equality, and its version set is part of the read contract: adding a version requires the old ones to stay readable.

## Verification Evidence

- `.venv/bin/python -m pytest` → `1185 passed, 25 skipped` on HEAD `54676a0`; `.venv-qlib/bin/python -m pytest` → `1210 passed, 0 skipped`. All 25 `.venv` skips are `could not import 'qlib'` / `Qlib extra is not installed`.
- Factor suites: `tests/test_factor_store.py`, `tests/test_factor_engine_contract.py`, `tests/test_factor_governance.py`, `tests/test_raw_price_engine.py`, `tests/test_factor_partition_set.py`, `tests/test_factor_set_input_line_v2.py`, `tests/test_factor_quality_single_source.py`, `tests/test_accepted_store_runtime.py`, `tests/test_model_store_isolation.py`, `tests/test_qlib_adapter.py` (Qlib-gated), `tests/test_publish_factor_partitions.py`.
- Contract-only factor sets are locked by the publish rejection tests in `tests/test_factor_store.py` and by `tests/test_factor_engine_contract.py`.

## Unknowns and Pending Confirmation

- `UNKNOWN`: whether Alpha360 has any planned implementation date. The repository records it as contract-only with no roadmap commitment visible in `specs/`.
- `UNKNOWN`: factor-layer official reference-price provenance is declared out of scope for release certification (`evidence/research-chain/release/release-record.json` `risk_limitations`).
