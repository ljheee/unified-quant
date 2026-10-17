#!/usr/bin/env python3
"""Publish governed label sets over a canonical bars window.

`LabelBuilder` computed labels and `LabelStore` read them back, but nothing could
write one, so this is the production entry point for the last pre-model family.
It reads canonical bars through the manifest-first reader, derives a 5-session
forward label per instrument, publishes the label set through `LabelSetWriter`,
and writes a reviewable report. Exit 1 if any instrument is refused.

The window must reach at least `horizon + 1` sessions *past the last decision
date*, because a decision date's label needs bars through D+horizon. Passing
exactly the decision window leaves the last `horizon` decision dates null, and
that is not an error: the label contract declares
``null_policy.insufficient_future = null``, so a decision date without enough
forward bars is a legitimate null label rather than a defect. The set is still
published, and the report names how many rows are null per instrument
(``null_labels``), so the tail is visible instead of being derived by hand.

Usage:
  scripts/build_label_sets.py --bars-root data/bars-a500-store \
      --label-root data/label-store \
      --start 2025-08-29 --end 2025-09-30 \
      --out evidence/.../label-sets.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.contracts.model_layer import sha256_json
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.models.label_store_writer import LabelSetWriter
from uq.models.labels import LabelBuilder
from uq.store.reader import ManifestFirstReader

_HORIZON = 5


def _window_sessions(store: Path, start: dt.date, end: dt.date) -> list[dt.date]:
    base = store / "canonical" / "bars_daily" / "v2"
    if not base.is_dir():
        raise SystemExit(f"missing canonical bars root: {base}")
    sessions = sorted(
        dt.date.fromisoformat(child.name.split("=", 1)[1])
        for child in base.iterdir()
        if child.is_dir() and child.name.startswith("date=")
    )
    return [session for session in sessions if start <= session <= end]


def _suffix_of(instrument: str) -> str:
    return {"XSHG": ".SH", "XSHE": ".SZ", "XBSE": ".BJ"}[instrument.split(".", 1)[1]]


def _listing_dates(instruments: list[str]) -> dict[str, dt.date]:
    """Listing dates from the provider security master (``stock_basic.list_date``).

    The canonical v2 field list carries no ``listing_date``, but the label
    eligibility rule needs a 60-day listing age.  ``stock_basic`` is the security
    master for this source, so the listing date comes from there rather than being
    invented.  A name the master does not cover is a refusal: guessing a listing
    date would be a silent point-in-time assumption about eligibility.
    """
    from uq.errors import ContractError

    adapter = Tushare2kAdapter()
    # Both statuses, merged. The default filter is ``L`` (listed), which silently
    # drops delisted names -- but a delisted instrument still needs a listing date
    # to decide label eligibility for the decision dates it *did* trade through.
    # Measured: 601989 (中国重工) is absent under ``L`` yet present under ``D`` with
    # ``list_date=20091216``, so the default filter alone makes a whole window
    # unlabellable for no reason.
    by_code: dict[str, str] = {}
    for status in ("L", "D"):
        master = adapter.stock_basic(list_status=status)
        if "ts_code" not in master.columns or "list_date" not in master.columns:
            raise SystemExit("security master lacks ts_code/list_date; cannot establish listing ages")
        # A live row wins over a delisted one: a code can appear in both sets if the
        # provider is laggard, and the listing date is the same anyway, so keeping
        # the first non-empty value is both deterministic and stable.
        for code, value in zip(master["ts_code"].astype(str), master["list_date"].astype(str)):
            if value and value != "nan":
                by_code.setdefault(code, value)
    out: dict[str, dt.date] = {}
    uncovered = []
    for instrument in instruments:
        code = instrument.split(".")[0] + _suffix_of(instrument)
        raw = by_code.get(code)
        if not raw:
            uncovered.append(instrument)
            continue
        try:
            out[instrument] = dt.date.fromisoformat(raw)
        except ValueError:
            uncovered.append(instrument)
    if uncovered:
        raise SystemExit(
            "no listing date for these instruments, so their label eligibility "
            f"cannot be established: {uncovered[:10]}"
        )
    return out


def _eligibility_columns(bars: pd.DataFrame, listing_dates: dict[str, dt.date]) -> pd.DataFrame:
    """Fill the label builder's eligibility columns.

    ``limit_up``/``limit_down`` are booleans in the label contract but the canonical
    v2 partition carries them as prices, so an actual limit event cannot be
    recognised here and both stay False (no limit event excluded).  ``suspended``
    is derived from ``status``.  This is a deliberate narrowing, recorded in the
    run report rather than presented as full eligibility.
    """
    frame = bars.copy()
    frame["suspended"] = frame["status"] != "trading"
    frame["limit_up"] = False
    frame["limit_down"] = False
    frame["delisted"] = False
    frame["listing_date"] = [
        pd.Timestamp(listing_dates[instrument], tz="UTC") for instrument in frame["instrument"]
    ]
    return frame


def _binding(frame: pd.DataFrame, instrument: str, schema: str) -> dict[str, Any]:
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort")
    # Must be the same function `LabelBuilder` verifies the binding against
    # (`sha256_json` over canonical JSON), or every publish would be refused on a
    # checksum that was computed a different way.
    checksum = sha256_json({"rows": [
        [str(row[0]), pd.Timestamp(row[1]).isoformat(), None if pd.isna(row[2]) else float(row[2]),
         None if pd.isna(row[3]) else float(row[3]), bool(row[4]), str(pd.Timestamp(row[5]).date())]
        for row in ordered[
            ["instrument", "datetime", "close", "adj_factor", "suspended", "listing_date"]
        ].itertuples(index=False)
    ]})
    # `label_set.v1` closes the binding object: no `instrument` field, and
    # `visible_cutoff` is required. The cutoff is the session close in Shanghai
    # time, which is when the bars this label is built from became knowable.
    session_close = ordered["datetime"].max().date().isoformat()
    return {
        "binding": "adjusted_price", "dataset": "bars_daily", "schema_version": schema,
        "partition_date": session_close,
        "generation_id": "0" * 64,
        "data_checksum_sha256": checksum,
        "visible_cutoff": f"{session_close}T15:00:00+08:00",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-root", type=Path, required=True)
    parser.add_argument("--label-root", type=Path, required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True,
                        help="first session of the bars window (not the decision window)")
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--schema", type=Path, default=Path("config/schemas/bars_daily.v2.yaml"))
    parser.add_argument("--name", default="return_5d")
    parser.add_argument("--semantic-version", default="1.0.0")
    parser.add_argument("--instrument", action="append", default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    sessions = _window_sessions(args.bars_root, args.start, args.end)
    if not sessions:
        raise SystemExit(f"no bars partitions in {args.start}..{args.end}")
    schema = load_schema(args.schema)
    reader = ManifestFirstReader(args.bars_root)
    bars = pd.concat([reader.read(schema, session) for session in sessions], ignore_index=True)

    instruments = sorted(set(bars["instrument"]))
    if args.instrument is not None:
        wanted = set(args.instrument)
        instruments = [name for name in instruments if name in wanted]
        missing = sorted(wanted - set(instruments))
        if missing:
            raise SystemExit(f"--instrument names not present in the window's bars: {missing}")
    if not instruments:
        raise SystemExit("no instruments to label")

    listing_dates = _listing_dates(instruments)
    # The label contract is keyed on `datetime`; the canonical v2 partition names
    # that column `session_date`. Rename at the boundary so the builder sees its own
    # contract, the same rename the factor layer makes.
    if "datetime" not in bars.columns and "session_date" in bars.columns:
        bars = bars.rename(columns={"session_date": "datetime"})
    eligibility = _eligibility_columns(bars, listing_dates)

    writer = LabelSetWriter(args.label_root)
    builder = LabelBuilder(name=args.name, semantic_version=args.semantic_version)
    published: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for instrument in instruments:
        frame = eligibility[eligibility["instrument"] == instrument]
        if frame.empty:
            refused.append({"instrument": instrument, "reason": "no bars in window"})
            continue
        try:
            manifest, output = builder.build_with_frame(
                frame, upstream_bindings=[_binding(frame, instrument, schema.name)]
            )
            identity = writer.publish(manifest, output)
        except ContractError as exc:
            refused.append({"instrument": instrument, "reason": str(exc)})
            continue
        null_labels = int(output["label"].isna().sum())
        published.append({
            "instrument": instrument,
            "label_generation_id": identity["generation_id"],
            "rows": identity["rows"],
            "null_labels": null_labels,
        })

    report = {
        "record_version": 1,
        "kind": "label_set_publication",
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat(), "sessions": len(sessions)},
        "stores": {
            "bars_root": str(args.bars_root),
            "label_root": str(args.label_root),
            "tracked": False,
            "note": "store roots are gitignored; the generation ids below are the reviewable identity",
        },
        "instruments_considered": len(instruments),
        "eligibility": {
            "listing_date_source": "tushare:stock_basic(list_date)",
            "listing_date_statuses": ["L", "D"],
            "limit_events": "not recognised; limit_up/limit_down are prices in the "
                            "canonical field list, so no limit day is excluded",
            "reproducibility_note": (
                "the upstream binding checksum covers listing_date, which the "
                "canonical v2 field list does not carry and which is therefore "
                "fetched live from stock_basic. A label set published here is "
                "reproducible only together with that listing_date mapping; the "
                "governed bars store alone does not determine the checksum."
            ),
        },
        "published": len(published),
        "refused": len(refused),
        "records": published,
        "refusals": refused,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "published": report["published"], "refused": report["refused"],
        "instruments_considered": len(instruments),
    }, ensure_ascii=False))
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
