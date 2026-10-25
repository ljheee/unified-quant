#!/usr/bin/env python3
"""Publish an expression-defined (Qlib) factor set over a canonical bars window.

`publish_factor_partitions.py` computes the raw-price sets (`basic`, `adjusted`)
in process and refuses expression-defined sets outright, pointing at "its
reviewed qlib entry point" -- which did not exist. This is that entry point.

Two things make this different from the raw-price publisher, and both are
forced by the reviewed definition rather than chosen here:

* **One computation, many partitions.** Alpha158's expressions are rolling
  statistics over 60 sessions, so the whole window has to be computed in one
  Qlib call and then split by date. Per-day calls would be both wrong (no
  history) and 220 times slower.
* **A complete instrument/date panel.** `QlibFactorAdapter` refuses a panel that
  is not the full cartesian product, and the canonical store has holes every
  session (suspensions, listings, delistings). This publisher therefore keeps
  only the instruments present on *every* session of the window -- the
  "complete-panel" rule -- and reports exactly which ones it dropped. On the
  2025 full-A window that is 5073 -> 5019 (1.1%), recorded in the output report.

Usage:
  scripts/publish_qlib_factor_partitions.py \
      --bars-root data/bars-csiall-store \
      --factor-root data/factors-csiall-alpha158 \
      --factor-set alpha158 --factor-version 2.0.0 \
      --start 2025-02-12 --end 2025-12-31 \
      --input-visibility session_close \
      --partition-set-out evidence/.../factor-partition-set.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.factors.engine import FactorEngine
from uq.factors.inputs import factor_input_frame
from uq.factors.partition_set import build_factor_partition_set
from uq.factors.qlib_adapter import QlibFactorAdapter
from uq.factors.store import FactorStore, factor_generation
from uq.store.reader import ManifestFirstReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DECISION_ZONE = ZoneInfo("Asia/Shanghai")
DECISION_CLOCK = dt.time(15, 0)


def _refuse(message: str) -> int:
    print(json.dumps({"status": "configuration_failure", "error": message}, ensure_ascii=False))
    return 2


def _canonical_partitions(root: Path) -> dict[str, list[dt.date]]:
    found: dict[str, list[dt.date]] = {}
    base = root / "canonical" / "bars_daily"
    if not base.is_dir():
        return found
    for dataset_dir in sorted(base.glob("*/")):
        sessions: list[dt.date] = []
        for partition in sorted(dataset_dir.glob("date=*/")):
            try:
                sessions.append(dt.date.fromisoformat(partition.name.split("=", 1)[1]))
            except ValueError:
                continue
        if sessions:
            found[dataset_dir.name] = sessions
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-root", type=Path, required=True)
    parser.add_argument("--factor-root", type=Path, required=True)
    parser.add_argument("--factor-set", required=True)
    parser.add_argument("--factor-version", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first session to publish")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last session to publish")
    parser.add_argument(
        "--input-visibility",
        required=True,
        choices=("session_close", "ingest_time"),
    )
    parser.add_argument("--partition-set-out", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
    except ValueError as exc:
        return _refuse(f"invalid --start/--end: {exc}")
    if end < start:
        return _refuse("--end precedes --start")

    registry = FactorRegistry(PROJECT_ROOT)
    try:
        definition = registry.get(args.factor_set, args.factor_version)
    except ContractError as exc:
        return _refuse(f"unknown or unreviewed factor set: {exc}")
    if "qlib_expression_set" not in definition.document:
        return _refuse(
            f"{args.factor_set}/{args.factor_version} is not an expression-defined set; "
            "publish it with scripts/publish_factor_partitions.py"
        )

    bindings = definition.document.get("input_bindings") or {}
    reviewed_versions = bindings.get("required_schema_versions", {}).get("bars_daily")
    if not reviewed_versions:
        return _refuse(
            f"{args.factor_set}/{args.factor_version} declares no reviewed bars_daily line"
        )

    present = _canonical_partitions(args.bars_root)
    if not present:
        return _refuse(f"no canonical bars_daily partitions under {args.bars_root}")
    lines_in_window = {
        version: [day for day in sessions if start <= day <= end]
        for version, sessions in present.items()
        if any(start <= day <= end for day in sessions)
    }
    if not lines_in_window:
        return _refuse(f"no canonical bars partitions inside {start}..{end}")
    unreviewed = sorted(set(lines_in_window) - set(reviewed_versions))
    if unreviewed:
        return _refuse(
            f"canonical store holds bars_daily/{', '.join(unreviewed)} inside the window "
            f"but {args.factor_set}/{args.factor_version} is reviewed only for "
            f"{', '.join(sorted(reviewed_versions))}"
        )
    if len(lines_in_window) > 1:
        return _refuse(f"mixed canonical lines inside the window: {sorted(lines_in_window)}")
    schema_version, target_days = next(iter(lines_in_window.items()))
    target_days = sorted(target_days)

    schema = load_schema(PROJECT_ROOT / "config" / "schemas" / f"bars_daily.{schema_version}.yaml")
    reader = ManifestFirstReader(args.bars_root)
    frames: list[pd.DataFrame] = []
    manifests: dict[dt.date, dict[str, Any]] = {}
    for day in target_days:
        frame = reader.read(schema, day)
        partition = (
            args.bars_root / "canonical" / "bars_daily" / schema_version
            / f"date={day.isoformat()}"
        )
        manifest = json.loads((partition / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("data_checksum_sha256") != hashlib.sha256(
            (partition / "data.parquet").read_bytes()
        ).hexdigest():
            return _refuse(f"canonical data checksum mismatch for {day}")
        projected, _ = factor_input_frame(
            frame,
            schema_version=schema_version,
            required_columns=definition.document["required_columns"],
        )
        frames.append(projected)
        manifests[day] = manifest
    panel = pd.concat(frames, ignore_index=True)

    # Complete-panel rule: QlibFactorAdapter refuses anything else, and the
    # canonical store has holes on every session. Keep only instruments present
    # on every session and report the exclusion rather than hiding it.
    counts = panel.groupby("instrument").size()
    instruments = sorted(counts[counts == len(target_days)].index)
    dropped = sorted(set(panel["instrument"].unique()) - set(instruments))
    if not instruments:
        return _refuse("no instrument has a complete panel across the window")
    panel = panel[panel["instrument"].isin(instruments)].reset_index(drop=True)

    adapter = QlibFactorAdapter(
        PROJECT_ROOT / "config" / "factor-sets"
        / f"{args.factor_set}-v{definition.factor_version.split('.')[0]}.json"
    )
    try:
        computed = adapter.compute(
            panel,
            instruments=instruments,
            start_date=target_days[0].isoformat(),
            end_date=target_days[-1].isoformat(),
        )
    except ContractError as exc:
        return _refuse(f"qlib factor computation refused: {exc}")

    store = FactorStore(args.factor_root, registry)
    engine = FactorEngine(
        args.factor_root,
        registry,
        run_visible_cutoff=dt.datetime.combine(
            target_days[-1], DECISION_CLOCK, tzinfo=DECISION_ZONE
        ),
    )
    report_root = args.factor_root
    published: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    # Alpha158's longest expressions look back `minimum_history` sessions, so the
    # first (minimum_history - 1) sessions of the window cannot produce a complete
    # factor vector: those factors are legitimately NaN and the reviewed policy is
    # `reject_all`. Publishing them would either be refused by the engine or, worse,
    # publish a frame the definition says is unacceptable. Skip the warm-up
    # explicitly and report it, rather than letting 60 sessions fail one by one.
    # `minimum_history = N` means the expression needs N *complete* sessions
    # before its value is defined, so the first N sessions of the window are
    # warm-up. Measured: with `N - 1` the 60th session still had `qlib_roc60`
    # all-NaN and the engine refused it.
    warmup = max(int(factor["minimum_history"]) for factor in definition.factors)
    warmup_days = target_days[:warmup]
    for day in warmup_days:
        skipped.append({
            "session": day.isoformat(),
            "reason": "warmup_insufficient_history",
            "detail": f"{warmup} sessions of history required before this factor set is complete",
        })

    for day in target_days[warmup:]:
        frame = computed[computed["datetime"] == pd.Timestamp(day)].reset_index(drop=True)
        if frame.empty:
            skipped.append({"session": day.isoformat(), "reason": "no_factor_rows"})
            continue
        bar_manifest = manifests[day]
        upstream_created_at = (
            dt.datetime.combine(day, DECISION_CLOCK, tzinfo=DECISION_ZONE)
            if args.input_visibility == "session_close"
            else dt.datetime.fromisoformat(str(bar_manifest["created_at"]))
        )
        arguments = {
            "factor_set": args.factor_set,
            "factor_version": args.factor_version,
            "frame": frame,
            "partition_date": day,
            "input_dataset": "bars_daily",
            "input_schema_version": schema_version,
            "upstream_generation_id": bar_manifest["generation_id"],
            "upstream_data_checksum": bar_manifest["data_checksum_sha256"],
            "quality_report_checksum": "",
            "upstream_created_at": upstream_created_at,
        }
        try:
            generation = factor_generation(**arguments)
            result = engine.create_result(
                engine.build_request(
                    trade_date=day,
                    factor_set=args.factor_set,
                    factor_version=args.factor_version,
                    session_dates=(day,),
                    intent="publication",
                    run_visible_cutoff=dt.datetime.combine(
                        day, DECISION_CLOCK, tzinfo=DECISION_ZONE
                    ),
                ),
                frame,
                input_lineage=(
                    {
                        "factor_set": args.factor_set,
                        "factor_version": args.factor_version,
                        "dataset": "bars_daily",
                        "schema_version": schema_version,
                        "generation_id": bar_manifest["generation_id"],
                        "data_checksum_sha256": bar_manifest["data_checksum_sha256"],
                    },
                ),
            )
            if result.status != "passed":
                raise ContractError(
                    f"governed factor engine refused {day}: status={result.status}"
                    + "".join(f"; {m}" for m in result.errors)
                )
            local_quality = result.quality_report
            report_document = {
                "report_version": 1,
                "binding_type": "factor_v1",
                "bound_generation_id": generation,
                "policy": local_quality["policy"],
                "status": local_quality["status"],
                "checks": local_quality["checks"],
                "errors": local_quality["errors"],
                "warnings": local_quality["warnings"],
            }
            report_store = QualityReportStore()
            report_path = report_root / "reports" / "factor_v1" / generation / "report.json"
            if report_path.exists():
                report_store.read(report_root, generation, binding_type="factor_v1")
                intended = json.dumps(
                    report_document, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                if report_path.read_bytes() != intended:
                    raise ContractError(
                        f"existing quality report for {day} disagrees with the recomputed decision"
                    )
            else:
                report_store.save(report_root, report_document)
            arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
            store.publish(**arguments)
            published.append({
                "session": day.isoformat(),
                "generation_id": generation,
                "rows": int(len(frame)),
            })
        except (ContractError, OSError, ValueError) as exc:
            if "immutable factor partition already published" in str(exc):
                skipped.append({"session": day.isoformat(), "reason": "already_published"})
                continue
            failed.append({"session": day.isoformat(), "error": f"{type(exc).__name__}: {exc}"})

    status = "failed" if failed else ("published" if published else "already_published")
    partition_set = None
    # Assemble the set from what is actually on disk, not from the requested
    # window: a re-run publishes nothing, and the warm-up prefix never has a
    # partition at all. Deriving the days from the store also makes the set
    # reproducible after an interrupted run.
    partition_root = (
        args.factor_root / "factors" / "dataset=bars_daily"
        / f"schema_version={schema_version}" / f"factor_set={args.factor_set}"
        / f"factor_version={args.factor_version}"
    )
    published_days = sorted(
        dt.date.fromisoformat(p.name.split("=", 1)[1])
        for p in partition_root.iterdir()
        if p.is_dir() and p.name.startswith("date=")
    ) if partition_root.is_dir() else []
    if args.partition_set_out is not None and published_days:
        partition_set = build_factor_partition_set(
            store,
            input_dataset="bars_daily",
            input_schema_version=schema_version,
            factor_set=args.factor_set,
            factor_version=args.factor_version,
            window_start=published_days[0],
            window_end=published_days[-1],
            trading_days=published_days,
        )
        args.partition_set_out.parent.mkdir(parents=True, exist_ok=True)
        args.partition_set_out.write_text(
            json.dumps(partition_set, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    report = {
        "record_version": 1,
        "kind": "qlib_factor_publication",
        "status": status,
        "factor_set": f"{args.factor_set}/{args.factor_version}",
        "input_line": f"bars_daily/{schema_version}",
        "input_visibility_basis": args.input_visibility,
        "window": {"start": target_days[0].isoformat(), "end": target_days[-1].isoformat()},
        "sessions": len(target_days),
        "panel": {
            "rows": int(len(panel)),
            "instruments": len(instruments),
            "excluded_incomplete_instruments": len(dropped),
            "excluded_sample": dropped[:20],
        },
        "published": published,
        "skipped": skipped,
        "failed": failed,
        "partition_set": partition_set,
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
    print(json.dumps({
        "status": status,
        "published": len(published),
        "skipped": len(skipped),
        "failed": len(failed),
        "instruments": len(instruments),
        "excluded_incomplete": len(dropped),
    }))
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
