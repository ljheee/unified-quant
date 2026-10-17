from __future__ import annotations

import hashlib
import json
import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from ..contracts.adjustment_snapshot import CONSUMABLE_FORMULA_VERSION
from ..errors import ContractError


_REQUIRED = ["instrument", "datetime", "close", "adj_factor"]
# cr.md U6/U17: no longer a dead duplicate — this names the same constant the
# snapshot reader enforces at the consumption seam (select_visible_generation),
# so the factor layer and the contract layer cannot drift apart again.
_LINEAGE_VERSION = CONSUMABLE_FORMULA_VERSION
_FACTOR_FORMULAS = {
    "return_1d": {"operation": "pct_change", "periods": 1},
    "return_5d": {"operation": "pct_change", "periods": 5},
    "return_20d": {"operation": "pct_change", "periods": 20},
    "volatility_20d": {"operation": "rolling_std", "periods": 20, "input": "return_1d"},
}


def _validate_binding(frame: pd.DataFrame, adjustment_snapshot_id: str, effective_date_table_checksum: str) -> None:
    missing = sorted(set(_REQUIRED) - set(frame.columns))
    if missing:
        raise ContractError(f"adjusted dependency absent: {missing}")
    # cr.md H7: close must be null-free too — a NaN close used to be pad-filled
    # inside pct_change, fabricating returns across missing sessions.
    if frame[["instrument", "datetime", "close", "adj_factor"]].isna().any().any():
        raise ContractError("adjusted dependency contains null values")
    if frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("duplicate canonical keys reject adjusted factor calculation")
    if not (frame["adj_factor"] > 0).all():
        raise ContractError("invalid non-positive adjustment factor")
    if not adjustment_snapshot_id or len(adjustment_snapshot_id) != 64:
        raise ContractError("adjustment lineage version mismatch")
    if not effective_date_table_checksum or len(effective_date_table_checksum) != 64:
        raise ContractError("effective-date table checksum mismatch")


def calculate_adjusted_factors(
    frame: pd.DataFrame,
    *,
    adjustment_snapshot_ids: list[str],
    effective_date_table_checksums: list[str],
    events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    _validate_binding(frame, adjustment_snapshot_ids[0], effective_date_table_checksums[0])
    # Cheap guard (docs/corporate-action-validation.md §6, option C): a factor that
    # is 1.0 everywhere *and* a close that never moves means the adjustment join
    # produced nothing at all -- no factor variation and no price variation cannot
    # both be right for a real instrument. Deliberately narrow: it does NOT reject
    # a flat factor on its own, because 62 of 5059 real instruments are legitimately
    # flat (they have never had a corporate action).
    if len(frame) > 1:
        flat_factor = bool((pd.to_numeric(frame["adj_factor"], errors="coerce") == 1.0).all())
        flat_close = bool(pd.to_numeric(frame["close"], errors="coerce").nunique() <= 1)
        if flat_factor and flat_close:
            raise ContractError(
                "adjusted input has a constant 1.0 adj_factor and a constant close; "
                "the adjustment join produced nothing"
            )
    # Event-table replay is the primary judgement when the caller supplies the
    # governed event table; without it the check degrades to the guards above.
    if events is not None:
        verify_against_events(frame, events)
    unique_snapshots = set(adjustment_snapshot_ids)
    unique_tables = set(effective_date_table_checksums)
    if len(unique_snapshots) != 1:
        raise ContractError("two snapshots inside one return/volatility window")
    if len(unique_tables) != 1:
        raise ContractError("effective-date table checksum mismatch inside one window")
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    expected_keys = set(map(tuple, ordered[["instrument", "datetime"]].to_numpy()))
    output = ordered[["instrument", "datetime"]].copy()
    adjusted_close = ordered["close"].astype(float) * ordered["adj_factor"].astype(float)

    def per_instrument(series: pd.Series, periods: int, operation: str = "return") -> pd.Series:
        # cr.md H7: fill_method=None — the pandas default pad-fills NaN closes
        # before differencing, fabricating returns across gaps.
        return series.groupby(ordered["instrument"], sort=False).transform(
            lambda values: values.pct_change(periods, fill_method=None) if operation == "return" else values.rolling(periods, min_periods=periods).std(ddof=1)
        )

    output["return_1d"] = per_instrument(adjusted_close, 1)
    output["return_5d"] = per_instrument(adjusted_close, 5)
    output["return_20d"] = per_instrument(adjusted_close, 20)
    output["volatility_20d"] = per_instrument(output["return_1d"], 20, "volatility")
    output = output.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    if set(map(tuple, output[["instrument", "datetime"]].to_numpy())) != expected_keys:
        raise ContractError("factor output key reconciliation failed")
    return output


def assert_not_raw_close(frame: pd.DataFrame, result: pd.DataFrame) -> None:
    """Test helper: fail when ``result`` reproduces raw-close returns.

    Deliberately **not** called from production. A blanket "adj_factor is 1.0
    everywhere" guard would be wrong: 62 of 5059 real instruments legitimately carry
    a flat 1.0 factor because they have never had a corporate action, so the check
    can only distinguish a substitution when it is given a frame whose factor is
    known to differ. It stays exported for the regression test that pins the
    substitution scenario (cr.md FC-M3).
    """
    if np.allclose(result["return_1d"].dropna(), frame.sort_values(["instrument","datetime"])["close"].pct_change(fill_method=None).dropna(), atol=1e-15, equal_nan=True):
        raise ContractError("raw close substituted for adjusted close")


def verify_against_events(
    frame: pd.DataFrame,
    events: pd.DataFrame,
    *,
    tolerance: float = 1e-3,
) -> dict[str, Any]:
    """Replay the corporate-action event table and check the factor jumps agree.

    This is the primary judgement described in ``docs/corporate-action-validation.md``
    §3.1: an event table is the only thing that can tell "this instrument is
    legitimately flat" apart from "the factor should have moved but did not",
    because a flat ``adj_factor`` is correct for the 62-of-5059 names that have
    never had a corporate action.

    The comparison is on **jumps, never levels**: derivations anchor the cumulative
    factor at different points (listing, window start, latest session), so two
    correct implementations can differ by a constant scale while agreeing on every
    jump.

    For each event date ``D`` present in both the events and the sessions, the
    expected jump is ``1 / (1 + shares + allot)`` for a pure share change, or the
    ex-right ratio from the exchange formula when cash is involved; the observed
    jump is ``adj_factor_D / adj_factor_{D-1}``. An event date whose session is
    absent from the frame is skipped and reported rather than failed -- a window
    that starts after an event legitimately does not contain it.
    """
    required = {"instrument", "datetime", "adj_factor"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ContractError(f"event replay requires frame columns: {missing}")
    if events is None or len(events) == 0:
        return {"checked_events": 0, "skipped_events": [], "tolerance": float(tolerance)}

    event_dates: dict[dt.date, tuple[float, float, float]] = {}
    for row in events.to_dict(orient="records"):
        day = dt.date(int(row["year"]), int(row["month"]), int(row["day"]))
        event_dates[day] = (
            float(row.get("fenhong") or 0.0),
            float(row.get("songzhuangu") or 0.0),
            float(row.get("peigu") or 0.0),
        )

    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort")
    checked = 0
    skipped: list[str] = []
    failures: list[str] = []
    for instrument, rows in ordered.groupby("instrument", sort=True):
        rows = rows.reset_index(drop=True)
        sessions = [pd.Timestamp(value).date() for value in rows["datetime"]]
        factors = pd.to_numeric(rows["adj_factor"], errors="coerce").to_numpy()
        closes = pd.to_numeric(rows["close"], errors="coerce").to_numpy() if "close" in rows else None
        for day, (fenhong, songzhuangu, peigu) in sorted(event_dates.items()):
            if day not in sessions:
                skipped.append(f"{instrument}@{day.isoformat()}")
                continue
            position = sessions.index(day)
            if position == 0:
                # No previous session in this window: the jump has no denominator.
                skipped.append(f"{instrument}@{day.isoformat()}")
                continue
            previous_factor, current_factor = factors[position - 1], factors[position]
            if not np.isfinite(previous_factor) or not np.isfinite(current_factor) or previous_factor <= 0:
                failures.append(f"{instrument}@{day.isoformat()}: unusable adj_factor")
                continue
            observed = float(current_factor) / float(previous_factor)
            shares = (songzhuangu + peigu) / 10.0
            cash = fenhong / 10.0
            if cash == 0.0:
                expected = 1.0 / (1.0 + shares) if shares else 1.0
            else:
                if closes is None or not np.isfinite(closes[position - 1]):
                    failures.append(f"{instrument}@{day.isoformat()}: cash event needs a prior close")
                    continue
                pre_close = float(closes[position - 1])
                reference = (pre_close - cash) / (1.0 + shares) if shares else pre_close - cash
                if reference <= 0:
                    failures.append(f"{instrument}@{day.isoformat()}: non-positive ex-right reference")
                    continue
                expected = pre_close / reference
            if abs(observed - expected) > tolerance * max(abs(expected), 1.0):
                failures.append(
                    f"{instrument}@{day.isoformat()}: factor jump {observed:.6f} "
                    f"vs event-implied {expected:.6f}"
                )
            checked += 1
    if failures:
        raise ContractError(
            "adjusted factor jumps disagree with the corporate-action event table: "
            + "; ".join(failures[:5])
        )
    return {"checked_events": checked, "skipped_events": skipped, "tolerance": float(tolerance)}
