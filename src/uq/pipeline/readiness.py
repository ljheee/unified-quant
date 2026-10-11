"""Pre-training readiness gate over governed inputs.

The training chain consumes three independently published inputs: PIT-visible
index membership, canonical daily bars, and the label horizon implied by the
dataset policy. Each of them can be individually well-formed while the
combination is unusable - a member with no bars silently shrinks the universe,
a short history silently drops names from the factor panel, and a label horizon
silently makes the last sessions untrainable. This module makes those
cross-input conditions explicit and fail-closed before a run is attempted.

It is deliberately pure: every input is passed in, so the rules can be tested
and re-evaluated without a network or a store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

import pandas as pd

# bars_daily v2 columns that must be present and non-null on every row.
_REQUIRED_BAR_FIELDS = (
    "instrument",
    "session_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "status",
    "adj_factor",
)
_TRADING_STATUS = "trading"


@dataclass(frozen=True)
class ReadinessFinding:
    code: str
    detail: str
    instruments: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "instruments": list(self.instruments)}


@dataclass(frozen=True)
class ReadinessReport:
    window_start: date
    window_end: date
    sessions: int
    members: int
    instruments_with_bars: int
    usable_label_end: date | None
    findings: tuple[ReadinessFinding, ...]
    metrics: Mapping[str, float] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "passed" if not self.findings else "blocked"

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "sessions": self.sessions,
            "members": self.members,
            "instruments_with_bars": self.instruments_with_bars,
            "usable_label_end": None if self.usable_label_end is None else self.usable_label_end.isoformat(),
            "metrics": dict(self.metrics),
            "findings": [finding.to_json() for finding in self.findings],
        }


def assess_readiness(
    *,
    window_start: date,
    window_end: date,
    sessions: Sequence[date],
    members_by_session: Mapping[date, Sequence[str]],
    bars: pd.DataFrame,
    min_sessions_per_instrument: int = 60,
    label_horizon_sessions: int = 5,
    max_null_rate: float = 0.0,
) -> ReadinessReport:
    """Evaluate the cross-input conditions a training run depends on."""
    session_values = sorted(set(sessions))
    findings: list[ReadinessFinding] = []
    metrics: dict[str, float] = {}

    if not session_values:
        findings.append(ReadinessFinding("no_sessions", "the window contains no trading sessions"))
    if window_start > window_end:
        findings.append(ReadinessFinding("invalid_window", "window start exceeds window end"))
    # Only out-of-window sessions are a finding. A window that ends on a
    # weekend or a holiday is normal input (month ends), and every downstream
    # rule keys off the sessions themselves, so requiring the last session to
    # equal window_end would mis-block a trainable window.
    outside = [value for value in session_values if value < window_start or value > window_end]
    if outside:
        findings.append(
            ReadinessFinding(
                "session_outside_window",
                f"{len(outside)} sessions fall outside {window_start}..{window_end}: "
                + ",".join(value.isoformat() for value in outside[:10]),
            )
        )

    missing_membership = [
        value.isoformat() for value in session_values if value not in members_by_session
    ]
    if missing_membership:
        findings.append(
            ReadinessFinding(
                "membership_unavailable",
                "no PIT-visible membership snapshot for sessions: " + ",".join(missing_membership[:10]),
            )
        )
    members = sorted({name for values in members_by_session.values() for name in values})
    if not members:
        findings.append(ReadinessFinding("empty_membership", "the membership universe is empty"))

    if bars.empty:
        findings.append(ReadinessFinding("no_bars", "canonical bars are empty for the window"))
        frame = pd.DataFrame(columns=list(_REQUIRED_BAR_FIELDS))
    else:
        frame = bars
        absent = [column for column in _REQUIRED_BAR_FIELDS if column not in frame.columns]
        if absent:
            findings.append(
                ReadinessFinding("bar_fields_missing", "canonical bars lack fields: " + ",".join(sorted(absent)))
            )

    observed_sessions = 0 if frame.empty else len({value for value in frame["session_date"]})
    if observed_sessions and observed_sessions != len(session_values):
        findings.append(
            ReadinessFinding(
                "bar_session_gap",
                f"bars cover {observed_sessions} sessions but the window declares {len(session_values)}",
            )
        )

    rows_by_instrument = {} if frame.empty else frame.groupby("instrument").size().to_dict()
    with_bars = sorted(rows_by_instrument)
    members_without_bars = sorted(set(members) - set(with_bars))
    # When the window has no bars at all, ``no_bars`` is the root cause; listing
    # every member as a second finding would only bury it.
    if members_without_bars and not frame.empty:
        findings.append(
            ReadinessFinding(
                "member_without_bars",
                f"{len(members_without_bars)} universe members have no bars in the window",
                tuple(members_without_bars[:50]),
            )
        )

    too_short = sorted(
        instrument
        for instrument, count in rows_by_instrument.items()
        if count < min_sessions_per_instrument
    )
    if too_short:
        findings.append(
            ReadinessFinding(
                "insufficient_history",
                f"{len(too_short)} instruments have fewer than {min_sessions_per_instrument} sessions",
                tuple(too_short[:50]),
            )
        )

    if not frame.empty:
        rows = len(frame)
        for column in _REQUIRED_BAR_FIELDS:
            if column not in frame.columns:
                continue
            null_rate = float(frame[column].isna().sum()) / rows
            metrics[f"null_rate.{column}"] = null_rate
            if null_rate > max_null_rate:
                findings.append(
                    ReadinessFinding(
                        "required_field_nulls",
                        f"{column} is null on {null_rate:.4%} of rows (allowed {max_null_rate:.4%})",
                    )
                )
        if "adj_factor" in frame.columns:
            invalid = frame["adj_factor"].notna() & (frame["adj_factor"] <= 0)
            if int(invalid.sum()):
                findings.append(
                    ReadinessFinding(
                        "non_positive_adjustment_factor",
                        f"adj_factor is non-positive on {int(invalid.sum())} rows",
                        tuple(sorted(set(frame.loc[invalid, "instrument"]))[:50]),
                    )
                )
        for column in ("open", "high", "low", "close"):
            if column in frame.columns:
                invalid = frame[column].notna() & (frame[column] <= 0)
                if int(invalid.sum()):
                    findings.append(
                        ReadinessFinding("non_positive_price", f"{column} is non-positive on {int(invalid.sum())} rows")
                    )
        # A missing limit price on a trading session means the backtest limit
        # rules cannot be evaluated, so the run would silently trade through a
        # limit that should have blocked the fill.
        if "limit_up" in frame.columns and "limit_down" in frame.columns and "status" in frame.columns:
            trading = frame["status"] == _TRADING_STATUS
            unbounded = trading & (frame["limit_up"].isna() | frame["limit_down"].isna())
            if int(unbounded.sum()):
                findings.append(
                    ReadinessFinding(
                        "limit_prices_missing",
                        f"{int(unbounded.sum())} trading rows lack limit_up/limit_down",
                        tuple(sorted(set(frame.loc[unbounded, "instrument"]))[:50]),
                    )
                )

    usable_label_end = _usable_label_end(session_values, label_horizon_sessions)
    if usable_label_end is None:
        findings.append(
            ReadinessFinding(
                "label_horizon_unsupported",
                f"the window has {len(session_values)} sessions, which cannot satisfy a "
                f"{label_horizon_sessions}-session label horizon",
            )
        )
    metrics["sessions"] = float(len(session_values))
    metrics["members"] = float(len(members))
    metrics["instruments_with_bars"] = float(len(with_bars))
    return ReadinessReport(
        window_start=window_start,
        window_end=window_end,
        sessions=len(session_values),
        members=len(members),
        instruments_with_bars=len(with_bars),
        usable_label_end=usable_label_end,
        findings=tuple(findings),
        metrics=metrics,
    )


def _usable_label_end(sessions: Sequence[date], label_horizon_sessions: int) -> date | None:
    """Last decision date whose forward label is fully covered by ``sessions``."""
    if label_horizon_sessions < 0 or len(sessions) <= label_horizon_sessions:
        return None
    return sessions[len(sessions) - label_horizon_sessions - 1]
