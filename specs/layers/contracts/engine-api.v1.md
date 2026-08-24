# Engine API Contract v1

Status: **frozen for Phase 2A planning**

## Normative request

`FactorComputeRequest` is the single semantic model. Required fields:

- `factor_set: str`
- `factor_version: str`
- `session_dates: tuple[date, ...]` or explicit `(start_date, end_date)`
- `universe_binding: UniverseSnapshot | None`
- `decision_time: datetime`
- `run_visible_cutoff: datetime`
- `serialization_profile_id: str | None`
- `intent: Literal["dry_run", "publication"]`

All defaults are resolved before validation and recorded in `request_metadata`.

## Public facade

Source-spec §10 remains the public facade:

```python
compute(trade_date, factor_set, factor_version, universe=None)
```

Facade resolution:

- `session_dates = (trade_date,)`;
- `universe_binding = None`; canonical keys are expected unless a governed universe is supplied;
- `decision_time = trade_date 15:00 Asia/Shanghai`;
- `run_visible_cutoff` is required from configuration; there is no implicit now;
- `serialization_profile_id` comes from the frozen factor-set config;
- `intent = "dry_run"`.

A separate typed method accepts an already constructed `FactorComputeRequest`.
The two entry points compile to the same execution plan.

## Result

`FactorResult.status` is one of `passed`, `warning`, `rejected`, `empty`.
Dry-run results remain in memory or repro-staging and cannot enter accepted
factor storage.
