# Ingest Routing and Canonical Store

> Current behavior reference. Last checked: `2026-10-20` against HEAD `54676a0a3b377d1b88da09309d96d6a7c6535419`. Based on source code, configuration, tests, and CI; unverified items are marked `UNKNOWN`.

## Responsibilities and Boundaries

This subsystem answers three questions and nothing else: **which source may serve a requested field set** (`src/uq/routing/router.py` + `src/uq/contracts/capabilities.py`), **how do multiple sources get merged and judged** (`src/uq/quality/gate.py`), and **how is the merged frame written immutably and read back verifiably** (`src/uq/store/pit_store.py`, `src/uq/store/reader.py`, `src/uq/contracts/canonical_v2.py`).

It does **not** own adapters, calendar providers, or lifecycle classification; those are injected (`src/uq/adapters/`, `src/uq/market/calendar.py`, `src/uq/market/lifecycle.py`) so their behaviour is verified separately. It does not own factor computation, model training, or the research-chain orchestration, all of which are consumers of the published partitions.

## Authoritative Sources

| Fact | Authoritative path |
| --- | --- |
| Declared source capabilities, primary source, owners, cross-validation rules | `config/datasets/*.yaml` parsed by `src/uq/contracts/config.py:22` |
| Field dtype / unit / null / invariant rules per dataset version | `config/schemas/*.yaml` parsed by `src/uq/contracts/schema.py:21` |
| Source eligibility and coverage verdict | `src/uq/routing/router.py:31` `SourceRouter.fetch` |
| Merge, per-field lineage, structured quality report | `src/uq/quality/gate.py:53` `CrossSourceGate.merge` |
| Immutable partition + manifest identity | `src/uq/store/pit_store.py:20` `CanonicalStore.publish`, `src/uq/contracts/canonical_v2.py:111` `CanonicalV2Store` |
| Governed read path | `src/uq/store/reader.py:15` `ManifestFirstReader.read` |
| Cross-store symlink guard | `src/uq/contracts/paths.py:32` `reject_symlinks_within` |
| JSON-Schema contract registry | `src/uq/contracts/gate_contracts.py:40` `validate_contract` over `config/schemas/contracts/*.json` |
| Raw supplemental Tushare snapshots | `src/uq/contracts/tushare_dependency.py` `TushareDependencyStore`, `config/schemas/contracts/tushare_dependency_snapshot.v1.json` |

`config/datasets/bars_daily.research-v1.yaml` is the currently executable research contract; `config/datasets/bars_daily.v2.yaml` is the production-draft line that carries `status`/`limit_up`/`limit_down`/`adj_factor`.

## Primary Runtime Paths

Entry point is the console script `uq-ingest` (`src/uq/cli.py:29`, registered as `uq.cli:uq_ingest`). `uq-ingest daily` takes one trading date, resolves its universe, builds a pipeline, and runs it.

1. **Universe resolution.** `--pipeline v1` reads the fixed whitelist `config/universe/research-whitelist.txt` (`src/uq/cli.py:92`); `--pipeline v2` reads PIT members from the membership store and requires `--membership-root` + `--universe-id` (`src/uq/cli.py:79`).
2. **Pipeline assembly** (`src/uq/pipeline/daily.py`): three builders exist — `build_tdx_pipeline` (line 91, TDX-only, the documented default credential-free path), `build_tushare_crosscheck_pipeline` (line 55, TDX primary + Tushare comparator + independent calendar), and `build_tushare_v2_pipeline` (line 114, Tushare primary on `bars_daily.v2`, TDX registered only when cross-validation is required).
3. **Health probe, then route.** `DailyIngestPipeline._run` (line 274) probes the primary adapter, calls `SourceRouter.fetch`, captures raw payload bytes when `row_policy.raw_capture` is set, and pins calendar evidence into the run report.
4. **Verified-only row policy.** Missing `(instrument, session)` keys are classified by `AkShareLifecycleProvider` (suspension/listing-delisting); an unexplained gap fails closed unless `allow_unknown_missing` is configured.
5. **Quality gate.** `CrossSourceGate.merge` compares secondary frames against the primary on declared tolerances, computes per-field `FieldCoverage` lineage, and rejects on conflicts, missing owner fields, or coverage below `minimum_coverage`.
6. **Publish.** `CanonicalStore.publish` writes a `date=YYYY-MM-DD.staging.<uuid>` directory, re-reads the Parquet, revalidates schema, computes data/schema checksums, then atomically renames. An existing partition is never overwritten: publication raises `publication_conflict`.
7. **Read.** `ManifestFirstReader.read` refuses a partition without a sibling manifest, re-verifies trust anchor, dataset/schema match, `partition_date`, data checksum, row count, column order, and per-column dtype.

Exit codes per status are declared in `EXIT_STATUS_CODES` (`src/uq/cli.py:19`) and are contract-tested by `tests/test_gate_exit_contracts.py`.

## Multiple Implementations / Entry Points

Three source-eligibility implementations are live and must not be conflated:

- **TDX-only research path** (`build_tdx_pipeline`, default `uq-ingest daily`): primary `tdx` via `src/uq/adapters/mootdx_source.py`. Its contract declares `cross_validation_mode: optional`, so cross-validation is skipped unless `--cross-validate` is passed. This is intentional: `mootdx` answers `xdxr` reliably but `bars`/`quotes` are frequently blocked on measured hosts, so probing with a quote-shaped command would wrongly mark the host unusable (`src/uq/adapters/mootdx_source.py:13`).
- **Cross-check research path** (`build_tushare_crosscheck_pipeline`): adds Tushare as comparator and an independent calendar; coverage resolves to `complete` rather than `unverified`.
- **`bars_daily.v2` production-draft path** (`build_tushare_v2_pipeline`): ownership inverts — Tushare is the only source serving `status`/`limit_up`/`limit_down`/`adj_factor`, so TDX can only be an OHLCV comparator. The Mootdx XDXR dividend cross-check for `adj_factor` is still not wired (`pendding.md`).

Two publication implementations coexist: the manifest-version-1 `CanonicalStore` used by the research-v1/v2 ingest pipelines, and `CanonicalV2Store` (`src/uq/contracts/canonical_v2.py:111`) used by later dataset/adjusted-price publication. Identity rules for canonical-v2 deliberately live on the governed read path (`src/uq/contracts/manifest.py:71`) so publisher and reader cannot drift.

## State and Data Contracts

Layout is `$DATA_ROOT/canonical/<dataset>/<schema_version>/date=<YYYY-MM-DD>/{data.parquet,manifest.json}`. Manifests are content-addressed: `generation_id` is `sha256` over the manifest without itself, and readers compare `trust_anchor_sha256` to `sha256(generation_id)`. Partitions are immutable; a re-publication requires a new run/partition and produces `publication_conflict` otherwise. A publication lock is taken per `(dataset, schema_version, date)` with `fcntl.flock`.

`bars_daily.research-v1` is keyed `(instrument, datetime)`; `bars_daily.v2` is keyed `(instrument, session_date)`. Raw prices and cumulative `adj_factor` are stored separately, never mixed in one column (`specs/architecture.md:3`).

Supplemental source dependencies such as daily indicators, industry membership, financial statements, seller expectations, money flow, connect holdings, and limit-up/down list details use a separate raw landing store at `$SOURCE_ROOT/tushare_dependencies/<endpoint>/generation=<id>/`. Its snapshot identity binds the endpoint, bounded query, and exact Parquet bytes; readers verify the manifest, row/column shape, and checksum. These raw provider records are not canonical factors, and this store does not assign availability timestamps or source-quality approval. A downstream publisher must normalize provider units, reconstruct a conservative point-in-time cutoff, and publish through its owning reviewed contract.

## Constraints, Failures, and Compatibility

- No complete source for the requested fields is a hard `CapabilityGapError`, never a silent partial. Partial output requires explicit `allow_partial` and is reported `coverage_status=partial` (`src/uq/routing/router.py:55`).
- A contract declaring `cross_validation_mode: required` makes `DailyIngestPipeline.__init__` raise if the run does not request cross-validation (`src/uq/pipeline/daily.py:200`).
- Degenerating the expected session set is refused: an independent-calendar provider that cannot answer raises `IndependentCalendarError` instead of returning an empty set (`src/uq/errors.py`), because an empty expected set would satisfy the coverage comparison and publish unverified data as complete.
- Unit or semantic schema changes require a new schema version (`AGENTS.md` Repository Instructions; `specs/architecture.md:3`).
- Evident historical instance: an in-place `const` flip in `adjustment_snapshot.v1.json` orphaned stored manifests and is recorded in `src/uq/contracts/adjustment_snapshot.py:23`.

## Verification Evidence

- `.venv/bin/python -m pytest` → `1185 passed, 25 skipped` on HEAD `54676a0` (25 skips are the Qlib extra not installed in `.venv`).
- `.venv-qlib/bin/python -m pytest` → `1210 passed, 0 skipped`.
- Targeted routing/gate/store coverage: `tests/test_acceptance.py` (5), `tests/test_pipeline_v2.py` (21), `tests/test_real_chain.py` (11), `tests/test_store_publication.py`, `tests/test_manifest_governance.py`, `tests/test_gate_exit_contracts.py`.
- Real-source adapters are exercised with fake clients in `tests/test_real_chain.py` and `tests/test_mootdx_source.py`; no test contacts a live source.
- `scripts/run_gate.sh` is the CI gate: it exports `uv.lock` to `.gate/requirements.lock.txt`, `uv sync --locked --extra dev --extra real`, then `python -m pytest`, and writes `.gate/gate-report.json`. Add `UQ_GATE_EXTRAS=qlib` for the Qlib cell. GitHub Actions runs it across macOS/Ubuntu × Python 3.11/3.12/3.13 (`.github/workflows/ci.yml`).

## Unknowns and Pending Confirmation

- `UNKNOWN`: whether any real ingest run has been executed against live TDX/Tushare in this worktree during bootstrap — none was run here, and no credentials were consumed. Evidence needed: a gate report or run report from an authorized session.
- `UNKNOWN`: `bars_daily.v2` publication coverage over a full A-share session — the line exists and is tested, but no committed full-market evidence was located. Evidence needed: an authorized `publish_market_datasets.py`/v2 ingest run report.
