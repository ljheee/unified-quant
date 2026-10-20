# Adjustment Factors and Corporate Actions

> Current behavior reference. Last checked: `2026-10-20` against HEAD `54676a0a3b377d1b88da09309d96d6a7c6535419`. Based on source code, configuration, tests, and documented measurements; unverified items are marked `UNKNOWN`.

## Responsibilities and Boundaries

This subsystem turns raw corporate-action/disclosure data into a governed **adjustment factor** lineage that the factor layer can bind, and publishes it as an immutable snapshot. It owns: event ingestion and normalization, exchange-shape event derivation, the cumulative factor chain, and the two-source event reconciliation gate.

It does **not** own raw OHLCV ingestion (`docs/subsystems/ingest-and-canonical-store.md`), factor computation over adjusted bars, or the research-chain binding resolution. It does not decide trading behaviour.

Two lineages exist with the same semantics (multiplicative basis, anchored to 1.0 at the latest session) and different provenance — they are recorded separately rather than merged:

| Lineage | Event source | Producer | Published snapshot version |
| --- | --- | --- | --- |
| `adj_factor.exchange_v2` | Tushare `dividend` disclosures mapped to exchange XDXR shape, only `div_proc == 实施` records | `src/uq/market/adjustment_chain.py:292` `build_adjustment_chain` | `3` |
| `adj_factor.derived_v1` | TDX `xdxr` — exchange-native events, no announcement mapping | `src/uq/market/adjustment_chain.py:396` `build_tdx_adjustment_chain` | `5` |

The TDX chain is a first-generation lineage, **not** a continuation of a `derived_vN` numbering that counts announcement-derived revisions (`docs/user-guide.md` §5.2).

## Authoritative Sources

| Fact | Authoritative path |
| --- | --- |
| Snapshot schema versions and consumable lineage set | `src/uq/contracts/adjustment_snapshot.py:48` `SNAPSHOT_SCHEMA_BY_VERSION`, `src/uq/contracts/adjustment_snapshot.py:80` `CONSUMABLE_FORMULA_VERSIONS` |
| Derivation rules and fail-closed conditions | module docstring `src/uq/market/adjustment_chain.py:1`, then `src/uq/market/adjustment_chain.py:119` `implemented_events`, `src/uq/market/adjustment_chain.py:237` `_provider_check` |
| Event store (announcement-anchored, append-only) | `src/uq/contracts/dividend_events.py:181` `DividendEventStore` |
| Two-source event reconciliation | `src/uq/market/corporate_action_reconcile.py:165` `reconcile_instruments` |
| Ingest of TDX XDXR events | `src/uq/adapters/mootdx_source.py:116` `fetch_xdxr_events` |
| Adjusted-price production line | `src/uq/pipeline/adjusted_price.py`, schema `config/schemas/bars_adjusted.adjusted-v2.yaml` |
| Measured divergence documentation | `docs/corporate-action-validation.md`, `docs/user-guide.md` §5.2 |

`docs/corporate-action-validation.md` is the authoritative write-up of the measurement method and evidence grades; this page records mechanism and current gate behaviour only.

## Primary Runtime Paths

Production entry points are scripts, not the CLI:

1. `scripts/build_tdx_adjustments.py` — fetch `xdxr` for a symbol window, normalize, publish the event table.
2. `scripts/fetch_dividend_events.py` — write the announcement-anchored dividend event store.
3. `scripts/build_dividend_adjustments.py` — map disclosures to exchange events and derive the factor chain.
4. `scripts/publish_adjusted_window.py` — publish `bars_adjusted/adjusted-v2` partitions, deriving restated prices from vendor `adj_factor`.
5. `scripts/reconcile_corporate_actions.py` — the routine event cross-check gate.

Derivation must satisfy, and refuses to publish otherwise:

- only implemented (`div_proc == 实施`) records are applied; a `stk_div` that disagrees with `stk_bo_rate + stk_co_rate` out of tolerance, or an `imp_ann_date` after its own `ex_date`, or an `ex_date` that is not a bar session, fails closed (`src/uq/market/adjustment_chain.py:11`);
- `cash_div_tax` (pre-tax) is used; a null pre-tax value fails closed rather than falling back to the after-tax field;
- Tushare per-share amounts are multiplied by ten for the exchange per-ten-share shape;
- rights issues (`peigu`/`peigujia`) are absent from the Tushare `dividend` endpoint, so they stay zero and the coverage gap is declared in provenance;
- the derived chain is cross-checked against the vendor cumulative `adj_factor`: `derived[t] == official[t]/official[last]` within tolerance, documented default `1e-3`, with divergence also reported as cash-per-share;
- non-zero `suogu` (share consolidation) is rejected rather than guessed, because consolidation reduces share count and folding it into the `1 + shares` denominator would produce an inverted ratio.

Snapshot publication dispatches on the manifest's own `snapshot_version` on the read side and on the declared lineage on the write side (`src/uq/market/adjustment_chain.py:357` `publish_adjustment_snapshot`), so a snapshot is always validated by the schema it names.

## Multiple Implementations / Entry Points

- **Two lineages, one consumer contract.** `CONSUMABLE_FORMULA_VERSIONS` is an explicit pair, not a wildcard; a reader accepts either and a snapshot records which one it was declared against. v2 and v3 snapshots (both `exchange_v2`) are equally consumable because the v3 change widened the instrument pattern to `.XBSE`.
- **Adjusted-price producers.** `src/uq/pipeline/adjusted_price.py` is the only producer of `bars_adjusted/adjusted-v2`; the legacy `src/uq/exporters/qlib.py` is explicitly deprecated and outside the model-layer accepted publication path.
- **Event reconciliation.** `scripts/reconcile_corporate_actions.py` reads the disclosure side from the *already published announcement store* (the same records the derived chain consumes) and the TDX side from `xdxr`; any divergence exits 1. This is a gate, not a log.
- Version numbers 4 and 5 are meaningful: 4 was used during development for the TDX lineage, deleted before release, and is deliberately retired rather than reused (`src/uq/contracts/adjustment_snapshot.py:45`).

## State and Data Contracts

Snapshots carry: instrument, session/effective-date table, the raw exchange events CSV, a checksum over the event table, `formula_version`, and `snapshot_version`. Choosing a generation uses `select_visible_generation`, whose lineage gate keeps a snapshot whose formula is not consumable from being read as current.

The event store is content-addressed and append-only; revisions are keyed by content, so byte-identical revisions collapse rather than duplicating a revision number.

## Event-table replay: what is compared, and against which instruments

`src/uq/factors/adjusted_price.py::verify_against_events` replays the event table against
the adjusted frame and compares **jumps, never levels** (derivations anchor the cumulative
factor at different points, so two correct implementations differ by a constant scale).

`events` accepts two shapes, and the distinction is load-bearing:

| Shape | Behaviour |
| --- |
| carries an `instrument` column | split by instrument; each instrument is replayed against **its own** events only |
| no `instrument` column | the table is treated as the events of **every** instrument in the frame |

A flat table replayed against every instrument is a false-failure machine: an instrument
with no corporate action has a flat factor *by right* (62 of 5059 real names), yet a foreign
ex-date would demand a jump it never made. The flat shape therefore remains valid only for
a single-instrument frame — which is what the production caller passes
(`src/uq/factors/engine.py:300` takes an instrument-agnostic table). `pandas`' `indicator`
is not used here; scoping is an explicit column filter over the instrument's spellings.

`checked_events` / `skipped_events` are returned for audit. **Unverified**: whether any
caller consumes `checked_events` today — the adjusted-window publisher records
`skipped_events` only (`scripts/publish_adjusted_window.py:292`).

## Constraints, Failures, and Compatibility
- **Semantic changes require a new snapshot version.** Documented precedents: v1→v2 for a factor-basis change, v2→v3 for the `.XBSE` pattern widening, v3→v5 for the TDX lineage. Each older version stays readable and keeps rejecting what it rejected, so existing partitions are never silently relabelled.
- **Tolerance is physical, not arbitrary.** The exchange publishes ex-right reference price at 0.01 CNY resolution and providers publish `adj_factor` at four decimals, so a purely relative jump tolerance consumed up to 89 % of the `1e-3` budget on a measured low-priced name and outright rejected a legal one. The gate now adds a floor of `tick / previous_close` (`src/uq/pipeline/adjusted_price.py`), which absorbs rounding without masking a genuine drift.
- **Structural gaps are declared, not counted as failures.** Because Tushare carries no rights-issue fields, `peigu`/`peigujia` `amount_diverged` rows only state that the disclosure side lacks the data; `fenhong`/`songzhuangu` divergences are the real two-source disagreements. Pure-bonus disclosures that cannot be mapped to the exchange event shape are counted as `uncovered` rather than failing the gate, so the gate does not turn a structural exclusion into a false red on most real instruments.
- **Beijing exchange reality.** TDX does not publish BSE events through the path that was originally assumed; that assumption was overturned by measurement (`cr.md` §8.44), and a dividend-store fallback exists for instruments TDX does not cover.

## Verification Evidence

- `.venv/bin/python -m pytest` → `1188 passed, 25 skipped`; `.venv-qlib/bin/python -m pytest` → `1213 passed, 0 skipped`. Counts updated by the change that adds the event-replay and missing-data-policy sections above.
- Adjustment-focused suites: `tests/test_adjustment_chain.py` (incl. the TDX lineage, v5/v3 mutual exclusion, consumption tests), `tests/test_adjustment_snapshot_runtime.py`, `tests/test_adjustment_cash.py`, `tests/test_adjustment_events.py`, `tests/test_build_tdx_adjustments.py`, `tests/test_corporate_action_reconcile.py`, `tests/test_reconcile_corporate_actions.py`, `tests/test_publish_adjusted_window.py`, `tests/test_adjusted_price_pipeline.py`.
- Snapshot-version coexistence is locked by `tests/test_gate_exit_contracts.py:300` `test_xbse_widening_lands_as_v3_not_an_in_place_edit` and by the v3/v5 mutual-exclusion tests in `tests/test_adjustment_chain.py`.
- The tick floor is locked by `tests/test_adjusted_price_pipeline.py:463` `test_the_tick_floor_absorbs_exchange_rounding_without_masking_a_drift`; removing the floor fails it.

## Unknowns and Pending Confirmation

- `UNKNOWN`: whether `suogu` ever carries a non-zero value in the TDX arrays this repository reads. The rejection is definitional (no consolidation formula is defined here) rather than statistical, and no measurement of the live column was possible without credentials — so no numeric claim is made.
- `UNKNOWN`: TDX coverage of Beijing-exchange events beyond the measured instruments. The earlier "TDX has no BSE events" conclusion was overturned once, so any per-specific claim needs its own probe with the correct command family.
