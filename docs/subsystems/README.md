# Subsystem Index

A subsystem page answers “how does the current system work, who is responsible for the behavior, and where can it be verified?” It is not a roadmap or implementation plan. Split documentation by stable responsibility boundaries: one subsystem may span multiple directories, and one directory may contain multiple subsystems. Create a separate page only when there is an independent responsibility, data/state contract, or boundary that is easy to modify incorrectly; avoid one document per file.

Use [`TEMPLATE.md`](TEMPLATE.md) when adding a page. Its section headings are the contract; "Unknowns and Pending Confirmation" is not optional filler.

## Subsystem List

Pages describe current behaviour as verified against HEAD `54676a0a3b377d1b88da09309d96d6a7c6535419` on 2026-10-20. "Last checked" on each page is the date that source was read, not a claim of running behaviour.

| Subsystem | Current responsibility / boundary | Authoritative documentation |
| --- | --- | --- |
| Ingest Routing and Canonical Store | Source eligibility by field set, cross-source merge and structured quality report, immutable partition publication and governed reads. Does not own adapters, calendars, or any consumer layer. | [ingest-and-canonical-store.md](ingest-and-canonical-store.md) |
| Adjustment Factors and Corporate Actions | Corporate-action event ingest, two governed factor lineages, exchange-shape derivation, two-source event reconciliation gate, adjusted-price line. Does not own raw OHLCV ingest or factor computation. | [adjustment-and-corporate-actions.md](adjustment-and-corporate-actions.md) |
| Factor Layer and Qlib Adapter | Governed factor-set registry, raw/adjusted factor computation, immutable factor partitions and logical fingerprints, reviewed Alpha158 computation in an isolated process. Does not own canonical partitions or labels. | [factor-layer-and-qlib-adapter.md](factor-layer-and-qlib-adapter.md) |
| Model Layer | Labels, feature schemas, training-sample construction, stateless cross-sectional preprocessing, model datasets, Qlib export/receipt, deterministic and real-Qlib training, prediction sets, all under externally reviewed quality decisions. | [model-layer.md](model-layer.md) |
| Portfolio, Backtest, Risk, and Paper Execution | Target weights, deterministic T+1 backtest, cross-cutting risk policy/decision governance, all-or-none paper fills and state evolution. Verified as one chain; broker/live execution explicitly out of scope. | [portfolio-backtest-risk-execution.md](portfolio-backtest-risk-execution.md) |
| Research Chain Orchestrator | Request validation, stage-plan review verification, input resolution through owning stores, stage execution with resume, result reconciliation. A governed orchestrator, not a computation engine; never signs quality decisions. | [research-chain.md](research-chain.md) |
| Data Sources, Calendars, and Readiness | Vendor adapters and fetch taxonomy, independent session calendar and its provenance, lifecycle classification of missing rows, pure fail-closed pre-training readiness gate. | [data-sources-and-readiness.md](data-sources-and-readiness.md) |

Cross-cutting concerns with no single owner page:

| Concern | Authoritative documentation |
| --- | --- |
| Contract schemas and identity rules | `src/uq/contracts/gate_contracts.py`, `config/schemas/contracts/*.json` |
| Symlink and path policy for governed stores | `src/uq/contracts/paths.py` |
| Production trust-anchor guard | `src/uq/runtime.py` |
| Canonical-v2 publication and migration ledger | `src/uq/contracts/canonical_v2.py` |
| Deprecated Qlib exporter (outside the accepted path) | `src/uq/exporters/qlib.py` |

## Modification Protocol

Before changing a subsystem, first read its page and the code standards, API/configuration contracts, and tests linked from it. If a change alters an entry point, call path, storage, state transition, dependency boundary, error/fallback behavior, permissions, security defaults, or compatibility, update the corresponding page and this index in the same change. Documentation describes verified current behavior; mark future ideas as proposals separately instead of mixing them with facts.

When describing a semantic change, state the trigger or input and set out the observable behavior before and after the change. Separately identify whether interfaces, state, compatibility, and failure behavior changed (yes/no/unknown); dimensions unrelated to this change need not be expanded, while unexamined dimensions remain `UNKNOWN` rather than being obscured by general wording.

Every important behavior should be traceable to a source path, public interface/configuration, or test. Use relative links and point to stable files and symbols where possible; if the repository supports line links and they will not drift frequently, line numbers may be added. Do not copy long code listings or write only abstract slogans. Keep concrete acceptance commands in `docs/project-profile.md` / `docs/code-standards.md`; pages retain only targeted verification relevant to that subsystem.

## How to Verify

- Every entry should have a concrete owner/implementation path and verification evidence, avoiding vague statements such as “handled by the backend.”
- Documentation links must exist in the repository; test commands must come from existing scripts, workflows, or maintainer confirmation.
- For critical paths, check state changes, failure branches, and compatibility rather than documenting only the success path.
- If a responsibility boundary is disputed, retain established facts, mark it `UNKNOWN`, and ask the project owner to confirm; do not split or merge modules on your own.
