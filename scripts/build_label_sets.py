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
      --delisting-snapshot-root data/tushare-dependencies \
      --delisting-snapshot-generation <stock-basic-D-generation> \
      --calendar-root data/backtest-market \
      --calendar-generation <governed-calendar-generation> \
      --calendar-checksum <governed-calendar-data-checksum> \
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
from uq.contracts.tushare_dependency import TushareDependencyStore
from uq.errors import ContractError
from uq.models.label_store_writer import LabelSetWriter
from uq.models.labels import LabelBuilder, adjusted_price_input_checksum
from uq.models.listing_snapshot import ListingDateSnapshotStore
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore
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


def _instrument_from_ts_code(code: str) -> str | None:
    # Tushare uses a leading T for Shanghai B-share codes (for example
    # T600018.SH). They cannot match this A-share canonical instrument key.
    if len(code) == 10 and code.startswith("T") and code[1:7].isdigit() and code[7:] == ".SH":
        return None
    try:
        number, exchange = code.rsplit(".", 1)
        suffix = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}[exchange]
    except (ValueError, KeyError) as exc:
        raise ContractError(f"invalid Tushare security code in delisting snapshot: {code!r}") from exc
    if len(number) != 6 or not number.isdigit():
        raise ContractError(f"invalid Tushare security code in delisting snapshot: {code!r}")
    return f"{number}.{suffix}"


def _delisting_dates(root: Path, generation_id: str) -> tuple[dict[str, dt.date], dict[str, Any]]:
    """Read delisting dates from one checksum-verified stock_basic(D) snapshot."""
    manifest, frame = TushareDependencyStore(root).read_frame(
        generation_id, dataset="stock_basic"
    )
    query = manifest.get("query", {})
    fields = {field.strip() for field in str(query.get("fields", "")).split(",")}
    required = {"ts_code", "list_status", "delist_date"}
    if (query.get("list_status") != "D" or not required <= fields
            or not required <= set(frame.columns)):
        raise ContractError("delisting snapshot must be stock_basic(D) with ts_code,list_status,delist_date")
    dates: dict[str, dt.date] = {}
    for code, status, raw_value in frame[["ts_code", "list_status", "delist_date"]].itertuples(index=False):
        if str(status) != "D" or pd.isna(raw_value) or not str(raw_value).strip():
            continue
        raw = str(raw_value).strip()
        try:
            day = (dt.date.fromisoformat(raw) if "-" in raw
                   else dt.datetime.strptime(raw, "%Y%m%d").date())
        except ValueError as exc:
            raise ContractError(f"invalid delist_date for {code}: {raw!r}") from exc
        instrument = _instrument_from_ts_code(str(code))
        if instrument is None:
            continue
        if instrument in dates and dates[instrument] != day:
            raise ContractError(f"conflicting delist_date rows for {instrument}")
        dates[instrument] = day
    return dates, manifest


def _governed_calendar(
    root: Path,
    generation_id: str,
    checksum: str,
    *,
    start: dt.date,
    end: dt.date,
) -> tuple[list[str], dict[str, Any]]:
    store = BacktestMarketDatasetStore(root)
    manifest = store.read_manifest_by(generation_id, dataset_type="calendar")
    if manifest.get("data_checksum_sha256") != checksum:
        raise ContractError("label calendar checksum does not match its manifest")
    expected_digest = sha256_json({
        key: value for key, value in manifest.items() if key != "manifest_digest_sha256"
    })
    if manifest.get("manifest_digest_sha256") != expected_digest:
        raise ContractError("label calendar manifest digest mismatch")
    frame = store.read_frame(
        generation_id, checksum, dataset_type="calendar",
        required_columns={"date", "instrument"},
    )
    if frame.empty or frame["instrument"].astype(str).tolist() != ["CALENDAR"] * len(frame):
        raise ContractError("label calendar must contain one CALENDAR row per session")
    dates = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d").tolist()
    if dates != sorted(set(dates)):
        raise ContractError("label calendar sessions must be sorted and unique")
    if not (manifest["window_start"] <= start.isoformat()
            and end.isoformat() <= manifest["window_end"]):
        raise ContractError("label window is outside the governed calendar")
    return dates, manifest


def _eligibility_columns(
    bars: pd.DataFrame,
    listing_dates: dict[str, dt.date],
    delisting_dates: dict[str, dt.date],
) -> pd.DataFrame:
    """Derive eligibility flags from raw canonical prices and pinned lifecycle data."""
    frame = bars.copy()
    frame["suspended"] = frame["status"] != "trading"
    close = pd.to_numeric(frame["close"], errors="raise").astype(float)
    limit_up = pd.to_numeric(frame["limit_up"], errors="coerce").astype(float)
    limit_down = pd.to_numeric(frame["limit_down"], errors="coerce").astype(float)
    # Both values use raw CNY; tolerance is well below one fen and only absorbs
    # floating-point representation noise.
    frame["limit_up"] = limit_up.notna() & (close - limit_up).abs().le(1e-8)
    frame["limit_down"] = limit_down.notna() & (close - limit_down).abs().le(1e-8)
    date_column = "datetime" if "datetime" in frame else "session_date"
    session_dates = pd.to_datetime(frame[date_column], errors="raise").dt.date
    frame["delisted"] = [
        instrument in delisting_dates and day >= delisting_dates[instrument]
        for instrument, day in zip(frame["instrument"], session_dates)
    ]
    frame["listing_date"] = [
        pd.Timestamp(listing_dates[instrument], tz="UTC") for instrument in frame["instrument"]
    ]
    return frame


def _binding(frame: pd.DataFrame, instrument: str, schema: str) -> dict[str, Any]:
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort")
    # Must be the same function `LabelBuilder` verifies the binding against
    # (`sha256_json` over canonical JSON), or every publish would be refused on a
    # checksum that was computed a different way.
    checksum = adjusted_price_input_checksum(ordered)
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
    parser.add_argument("--semantic-version", default="2.0.0")
    parser.add_argument(
        "--listing-snapshot-generation",
        help="reuse a pinned listing-date map in --label-root without any vendor call",
    )
    parser.add_argument("--delisting-snapshot-root", type=Path,
                        help="Tushare dependency store with a stock_basic(D) snapshot")
    parser.add_argument("--delisting-snapshot-generation",
                        help="checksum-verified stock_basic(D) snapshot generation")
    parser.add_argument("--calendar-root", type=Path,
                        help="BacktestMarketDatasetStore with an independently checked calendar")
    parser.add_argument("--calendar-generation", help="governed calendar generation")
    parser.add_argument("--calendar-checksum", help="governed calendar data checksum")
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
    if (args.delisting_snapshot_root is None) != (args.delisting_snapshot_generation is None):
        raise ContractError("delisting snapshot root and generation must be supplied together")
    if args.delisting_snapshot_root is None:
        raise ContractError("label publication requires a pinned stock_basic(D) delisting snapshot")
    delisting_dates, delisting_manifest = _delisting_dates(
        args.delisting_snapshot_root, args.delisting_snapshot_generation
    )
    if (args.calendar_root is None or args.calendar_generation is None
            or args.calendar_checksum is None):
        raise ContractError("label publication requires a governed calendar root, generation, and checksum")
    calendar_dates, calendar_manifest = _governed_calendar(
        args.calendar_root, args.calendar_generation, args.calendar_checksum,
        start=sessions[0], end=sessions[-1],
    )
    # A restricted publication must not ask eligibility to resolve unselected names.
    bars = bars[bars["instrument"].isin(instruments)].copy()
    # The label contract is keyed on `datetime`; the canonical v2 partition names
    # that column `session_date`. Rename at the boundary so the builder sees its own
    # contract, the same rename the factor layer makes.
    if "datetime" not in bars.columns and "session_date" in bars.columns:
        bars = bars.rename(columns={"session_date": "datetime"})
    bar_dates = pd.to_datetime(bars["datetime"], errors="raise").dt.strftime("%Y-%m-%d")
    if not set(bar_dates) <= set(calendar_dates):
        raise ContractError("label input contains a bar date absent from the governed calendar")
    eligibility = _eligibility_columns(bars, listing_dates, delisting_dates)

    writer = LabelSetWriter(args.label_root)
    builder = LabelBuilder(
        name=args.name,
        semantic_version=args.semantic_version,
        eligibility_provenance={
            "listing_date_snapshot_generation_id": snapshot["generation_id"],
            "delisting_snapshot_generation_id": delisting_manifest["generation_id"],
            "delisting_snapshot_data_checksum_sha256": delisting_manifest["data_artifact"]["checksum_sha256"],
        },
        calendar_dates=calendar_dates,
        calendar_binding={
            "generation_id": calendar_manifest["generation_id"],
            "data_checksum_sha256": calendar_manifest["data_checksum_sha256"],
            "manifest_digest_sha256": calendar_manifest["manifest_digest_sha256"],
        },
    )
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

    # A research request binds one label set for the whole universe. The combined
    # path computes the calendar grid once; previously it scanned the full market
    # frame once per instrument, built thousands of small label sets, then rebuilt
    # their concatenation. Keep the explicit per-instrument mode, but group once so
    # that path also scales linearly with the input rows.
    if args.combined:
        built = _build("<combined>", eligibility)
        if built is not None:
            manifest, output = built
            eligible_instruments = set(output["instrument"].astype(str).unique())
            refused.extend(
                {
                    "instrument": instrument,
                    "reason": "no eligible label rows after eligibility filtering",
                }
                for instrument in instruments
                if instrument not in eligible_instruments
            )
            if not output.empty:
                identity = writer.publish(manifest, output)
                published.append({
                    "instrument": "<combined>",
                    "label_generation_id": identity["generation_id"],
                    "rows": identity["rows"],
                    "null_labels": int(output["label"].isna().sum()),
                })
    else:
        grouped_frames = {
            str(instrument): group
            for instrument, group in eligibility.groupby("instrument", sort=False)
        }
        for instrument in instruments:
            frame = grouped_frames.get(instrument)
            if frame is None or frame.empty:
                refused.append({"instrument": instrument, "reason": "no bars in window"})
                continue
            built = _build(instrument, frame)
            if built is None:
                continue
            manifest, output = built
            if output.empty:
                refused.append({
                    "instrument": instrument,
                    "reason": "no eligible label rows after eligibility filtering",
                })
                continue
            identity = writer.publish(manifest, output)
            published.append({
                "instrument": instrument,
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
            "limit_events": "recognised when close equals canonical raw limit_up or limit_down",
            "delisting_snapshot": {
                "generation_id": delisting_manifest["generation_id"],
                "data_checksum_sha256": delisting_manifest["data_artifact"]["checksum_sha256"],
                "source_fetched_at": delisting_manifest["source_fetched_at"],
            },
            "calendar_binding": {
                "generation_id": calendar_manifest["generation_id"],
                "data_checksum_sha256": calendar_manifest["data_checksum_sha256"],
                "manifest_digest_sha256": calendar_manifest["manifest_digest_sha256"],
            },
            "reproducibility_note": (
                "the label manifest binds the governed session calendar and the "
                "upstream checksum covers listing_date and derived suspension/limit/"
                "delisting flags; the exact listing map "
                "is preserved in the immutable listing_date_snapshot. Replay with "
                "--listing-snapshot-generation, the pinned delisting snapshot, and "
                "the calendar generation/checksum to avoid live source calls. "
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
