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
from uq.models.listing_snapshot import ListingDateSnapshotStore
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
    parser.add_argument(
        "--listing-snapshot-generation",
        help="reuse a pinned listing-date map in --label-root without any vendor call",
    )
    parser.add_argument("--instrument", action="append", default=None,
                        help="restrict the DEFAULT per-instrument publication to these names")
    parser.add_argument(
        "--combined",
        action="store_true",
        help=(
            "publish ONE label set covering every instrument instead of one set per "
            "instrument. The research chain binds a single label set for the whole "
            "universe, so per-instrument sets cannot feed a v5 execute."
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    schema = load_schema(args.schema)
    if (schema.dataset, schema.version) != ("bars_daily", "v2"):
        raise ContractError(
            "label v1 formula expects raw bars_daily/v2 prices and applies adj_factor once"
        )
    sessions = _window_sessions(args.bars_root, args.start, args.end)
    if not sessions:
        raise SystemExit(f"no bars partitions in {args.start}..{args.end}")
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

    snapshot_store = ListingDateSnapshotStore(args.label_root)
    if args.listing_snapshot_generation:
        snapshot, listing_dates = snapshot_store.read(args.listing_snapshot_generation)
        uncovered = sorted(set(instruments) - set(listing_dates))
        if uncovered:
            raise ContractError(f"listing snapshot does not cover instruments: {uncovered[:10]}")
    else:
        listing_dates = _listing_dates(instruments)
        uncovered = sorted(set(instruments) - set(listing_dates))
        if uncovered:
            raise ContractError(f"listing dates do not cover instruments: {uncovered[:10]}")
        snapshot = snapshot_store.publish(
            listing_dates, source="tushare:stock_basic(list_date;list_status=L,D)"
        )
    # A restricted publication must not ask eligibility to resolve unselected names.
    bars = bars[bars["instrument"].isin(instruments)].copy()
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

    def _build(instrument: str, frame: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame] | None:
        """Run one group through the builder, recording a refusal when it fails.

        The upstream binding must describe ``frame`` itself: the builder recomputes
        the checksum over whatever frame it is handed and refuses a mismatch, so
        this checksum is not decorative.
        """
        try:
            return builder.build_with_frame(
                frame, upstream_bindings=[_binding(frame, instrument, schema.name)]
            )
        except ContractError as exc:
            refused.append({"instrument": instrument, "reason": str(exc)})
            return None

    # A research request binds exactly one label set for its whole universe, so the
    # per-instrument sets this script publishes by default can never be bound by a
    # v5 execute: `LabelStore` is keyed on one generation id and
    # `upstream_adjusted_price_bindings` is a list of adjusted-price bindings, not of
    # label sets. `--combined` therefore builds per instrument first and publishes the
    # concatenation of the survivors as a single set.
    #
    # Building per instrument is not wasteful ceremony. `LabelBuilder` filters rows
    # by eligibility (suspension, 60-day listing age, limit, delisting) *after* it
    # computes the grid, so an instrument ineligible on every date yields a zero-row
    # set. Per-instrument mode reports that as a named refusal; concatenating the
    # *input* frames first would drop it silently and still exit 0, so the refusal
    # would never reach the report. The combined set is the concatenation of the
    # frames that actually produced labels, and every dropped instrument stays named.
    folded: list[pd.DataFrame] = []
    for instrument in instruments:
        frame = eligibility[eligibility["instrument"] == instrument]
        if frame.empty:
            # Unreachable while `instruments` is derived from the bars frame; kept
            # so the guard survives that assumption changing.
            refused.append({"instrument": instrument, "reason": "no bars in window"})
            continue
        built = _build(instrument, frame)
        if built is None:
            continue
        _manifest, output = built
        if output.empty:
            refused.append({
                "instrument": instrument,
                "reason": "no eligible label rows after eligibility filtering",
            })
            continue
        if args.combined:
            folded.append(frame)
        else:
            identity = writer.publish(_manifest, output)
            published.append({
                "instrument": instrument,
                "label_generation_id": identity["generation_id"],
                "rows": identity["rows"],
                "null_labels": int(output["label"].isna().sum()),
            })

    if args.combined:
        if not folded:
            raise SystemExit("no instrument in the window produced an eligible label row")
        # One build over the concatenation is what makes the binding honest: the
        # checksum covers the same rows the manifest's `row_count` describes, and
        # the identity is the identity of this frame rather than of some instrument.
        combined = pd.concat(folded, ignore_index=True)
        manifest, output = _build("<combined>", combined)
        assert manifest is not None  # a refused concatenation was already recorded
        identity = writer.publish(manifest, output)
        published.append({
            "instrument": "<combined>",
            "label_generation_id": identity["generation_id"],
            "rows": identity["rows"],
            "null_labels": int(output["label"].isna().sum()),
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
        "mode": "combined" if args.combined else "per_instrument",
        "eligibility": {
            "listing_date_source": snapshot["source"],
            "listing_date_statuses": ["L", "D"],
            "listing_date_snapshot": {
                "generation_id": snapshot["generation_id"],
                "path": str(snapshot_store.path_for(snapshot["generation_id"])),
            },
            "limit_events": "not recognised; limit_up/limit_down are prices in the "
                            "canonical field list, so no limit day is excluded",
            "reproducibility_note": (
                "the upstream binding checksum covers listing_date; the exact map "
                "is preserved in the immutable listing_date_snapshot. Replay with "
                "--listing-snapshot-generation to avoid a live security-master fetch. "
                "This map does not certify historical PIT universe completeness."
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
