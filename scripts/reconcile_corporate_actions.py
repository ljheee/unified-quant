#!/usr/bin/env python3
"""Reconcile the two corporate-action event sources over an instrument set.

Both adjustment derivations are separately reconciled against the vendor's own
cumulative ``adj_factor``. That leaves a real hole: if the vendor's factor itself
is wrong, both derivations agree with it and the error passes. This entry point
compares the two *event sources* directly -- Tushare ``dividend`` disclosures
mapped into the exchange event shape, against TDX ``xdxr`` as published -- so a
wrong disclosure (or an event only one vendor knows about) is visible.

It reads announcements from the governed dividend store rather than the API, so
the disclosure side is the same announcement-anchored record the derivation
consumes. Exit 1 if any instrument diverges, so the report is a gate rather than
a log.

An instrument with **no comparable event at all** is a failure too, not a pass:
two sources that agree on zero events have not been reconciled. That covers both
the one-sided case (disclosure carries events, TDX has none) and the fully
disjoint one (both sides non-empty but sharing no ex-date), since a date only one
source knows about is exactly the missing event that silently under-adjusts.

The report never names a winner: which side is right is settled by the vendor
factor jump, which the adjustment chain checks separately.

Usage:
  scripts/reconcile_corporate_actions.py --dividend-root data/dividend-store \
      --instruments 600000.XSHG 000001.XSHE \
      --out evidence/reference-data/corporate-action-reconciliation.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.adapters.mootdx_source import MootdxSourceAdapter
from uq.contracts.dividend_events import DividendEventStore
from uq.market.adjustment_chain import EVENT_COLUMNS, implemented_events
from uq.market.corporate_action_reconcile import reconcile_instrument, summarize


def _disclosure_events(
    records: pd.DataFrame, instrument: str, as_of: dt.date
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Map one instrument's announcements into the exchange event shape.

    Returns ``(events, notes)``. The derivation's own mapping fails closed on a
    share-only distribution, because deriving a factor from it needs a pre-tax cash
    amount. That refusal is correct for derivation but wrong for *comparison*: a
    share-only event is exactly as comparable as any other, and measured on the
    vendor's own disclosures it is 157 of 4724 implemented records. Refusing the
    whole instrument over them would leave the gate unable to cover most names.

    So a first pass is attempted over everything; if it fails, the instrument is
    re-run over the records the mapping can express and the excluded dates are
    reported as ``uncovered`` rather than silently dropped.

    Each note carries a ``kind``: ``uncovered`` records a disclosure the comparison
    could not express but that did not stop the instrument from being compared, and
    ``refused`` records an instrument with nothing comparable at all. Only the
    latter is a gate failure -- an uncovered record is a known structural exclusion,
    not a defect, and folding it into the failure count would fail the gate on most
    real names.
    """
    window = {"window_start": dt.date(1990, 1, 1), "window_end": as_of}
    try:
        events, _dropped = implemented_events(records, instrument=instrument, **window)
        return events[list(EVENT_COLUMNS)], []
    except Exception as exc:
        first_failure = f"{type(exc).__name__}: {exc}"

    own = records[records["instrument"] == instrument]
    shares = own[
        own["div_proc"].eq("实施")
        & own["ex_date"].notna()
        & pd.to_numeric(own["cash_div_tax"], errors="coerce").notna()
    ]
    excluded = sorted(
        str(value) for value in own[
            own["div_proc"].eq("实施") & own["ex_date"].notna()
        ]["ex_date"].unique()
        if str(value) not in set(shares["ex_date"].astype(str))
    )
    if shares.empty:
        return pd.DataFrame(columns=list(EVENT_COLUMNS)), [
            {"instrument": instrument, "side": "disclosure", "kind": "refused", "reason": first_failure}
        ]
    try:
        events, _dropped = implemented_events(shares, instrument=instrument, **window)
    except Exception as exc:
        # The reduced set still fails, so the disclosure side is not merely
        # share-only: it is genuinely unmappable (a dirty record, or two revisions
        # that disagree on the same ex-date). Report it as a refusal rather than
        # letting the traceback abort the whole run.
        return pd.DataFrame(columns=list(EVENT_COLUMNS)), [
            {"instrument": instrument, "side": "disclosure", "kind": "refused",
             "reason": f"{type(exc).__name__}: {exc}"}
        ]
    return events[list(EVENT_COLUMNS)], [
        {
            "instrument": instrument,
            "side": "disclosure",
            "kind": "uncovered",
            "reason": "share-only or otherwise unmappable disclosures excluded from the comparison",
            "excluded_ex_dates": excluded[:50],
        }
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dividend-root", type=Path, required=True)
    parser.add_argument("--instruments", nargs="+", required=True)
    parser.add_argument("--as-of", type=dt.date.fromisoformat, default=None,
                        help="announcement cutoff for the disclosure side (default: today)")
    parser.add_argument("--tolerance", type=float, default=1e-6,
                        help="per-10-share amount tolerance; TDX publishes amounts per 10 shares")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    as_of = args.as_of or dt.date.today()
    records = DividendEventStore(args.dividend_root).as_of(as_of)
    if records.empty:
        raise SystemExit(f"no dividend disclosures on or before {as_of}")

    adapter = MootdxSourceAdapter()
    symbols = [instrument.split(".")[0] for instrument in args.instruments]
    try:
        frames = adapter.fetch_xdxr_events(symbols)
    except Exception as exc:
        raise SystemExit(f"TDX xdxr fetch failed: {exc}")

    results = []
    refusals: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    for instrument in args.instruments:
        disclosure, notes = _disclosure_events(
            records=records, instrument=instrument, as_of=as_of
        )
        refusals.extend(note for note in notes if note["kind"] == "refused")
        uncovered.extend(note for note in notes if note["kind"] == "uncovered")
        if disclosure.empty:
            continue
        raw = frames.get(instrument.split(".")[0], pd.DataFrame())
        # Only `category == 1` (除权除息) rows carry ex-right meaning: TDX also emits
        # 股本变化 rows that describe share-count drift and have no ex-right
        # meaning. Filter them here rather than passing the frame through when the
        # column happens to be absent -- a missing category column means the events
        # cannot be classified at all, so nothing is comparable.
        if "category" not in raw.columns:
            refusals.append({
                "instrument": instrument, "side": "tdx", "kind": "refused",
                "reason": "tdx xdxr frame carries no category column, so its events "
                          "cannot be classified as ex-right",
            })
            continue
        tdx = raw[raw["category"] == 1]
        results.append(reconcile_instrument(
            instrument, disclosure, tdx[list(EVENT_COLUMNS)], tolerance=args.tolerance
        ))

    summary = summarize(results)
    report = {
        "record_version": 1,
        "kind": "corporate_action_reconciliation",
        "as_of": as_of.isoformat(),
        "stores": {
            "dividend_root": str(args.dividend_root),
            "tracked": False,
            "note": "store roots are gitignored; this report is the reviewable identity",
        },
        "sources": {
            "disclosure": "tushare:dividend (announcement-anchored store, mapped to exchange shape)",
            "events": "tdx:xdxr",
            "server": adapter.connected_server,
        },
        "tolerance_per_ten_shares": args.tolerance,
        "instruments_requested": len(args.instruments),
        "instruments_compared": summary["instruments_compared"],
        "agreed": summary["agreed"],
        "amount_diverged": summary["amount_diverged"],
        "coverage_diverged": summary["coverage_diverged"],
        "no_overlap": summary["no_overlap"],
        "amount_diverged_instruments": summary["amount_diverged_instruments"],
        "coverage_diverged_instruments": summary["coverage_diverged_instruments"],
        "no_overlap_instruments": summary["no_overlap_instruments"],
        "records": summary["records"],
        "uncovered": uncovered,
        "refusals": refusals,
        "verdict_note": (
            "this report does not decide which source is right; that is settled by "
            "the vendor adj_factor jump, which the adjustment chain checks separately"
        ),
        "structural_note": (
            "`peigu`/`peigujia` (rights issues) are not carried by the tushare "
            "`dividend` endpoint at all, so a rights issue can only ever appear as "
            "a peigu/peigujia divergence -- that is a known coverage gap in the "
            "disclosure source, not a value dispute. A peigu/peigujia divergence "
            "means the disclosure side has no rights data, while a fenhong/"
            "songzhuangu divergence means the two sources disagree on an amount "
            "both carry."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "instruments_compared": report["instruments_compared"],
        "agreed": report["agreed"],
        "amount_diverged": report["amount_diverged"],
        "coverage_diverged": report["coverage_diverged"],
        "no_overlap": report["no_overlap"],
        "amount_diverged_instruments": report["amount_diverged_instruments"],
        "coverage_diverged_instruments": report["coverage_diverged_instruments"],
        "no_overlap_instruments": summary["no_overlap_instruments"],
        "refusals": len(refusals),
    }, ensure_ascii=False))
    # An instrument with zero comparable events is a gate failure, not a pass: two
    # sources that agree on nothing have not been reconciled at all, and the
    # disjoint case (both sides non-empty, no shared ex-date) is exactly the
    # "a missing event silently under-adjusts" failure this check exists to catch.
    diverged = (
        summary["amount_diverged"] + summary["coverage_diverged"] + summary["no_overlap"]
    )
    return 1 if diverged or refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
