# Adjustment Snapshot Storage & Reader v1

## Layout

```text
$UQ_DATA_ROOT/
  adjustments/
    snapshots/
      <instrument>/
        <generation_id>/
          manifest.json
          events.csv
          effective_dates.csv
          sessions.csv
```

`manifest.json` conforms to `adjustment_snapshot.v1.json`. Artifact paths in the
manifest are relative to the generation directory and MUST be checksum-bound.

## Visibility / as-of

A reader call supplies `as_of_time`. Only snapshots with
`visibility_time <= as_of_time` are eligible. For one instrument and window,
select exactly one `generation_id`; if multiple visible snapshots exist, the
caller must explicitly bind the governed selection from the factor request—readers do not silently choose latest.

## Typed reader interface

```python
class AdjustmentSnapshotReader:
    def read_manifest(self, instrument: str, generation_id: str) -> dict: ...
    def read_events(self, instrument: str, generation_id: str) -> pd.DataFrame: ...
    def read_effective_dates(self, instrument: str, generation_id: str) -> pd.DataFrame: ...
    def select_visible_generation(self, *, instrument: str, window_start: date, window_end: date, as_of_time: datetime) -> str: ...
```

Fail closed on absent artifacts, checksum mismatch, malformed CSV columns,
window outside session coverage, invisible snapshot, and ambiguous generation
selection.
