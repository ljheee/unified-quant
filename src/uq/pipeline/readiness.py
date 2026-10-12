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

    ``suspensions`` is the governed suspension source. It is authoritative only
    for the instrument/session pairs it contains, so a caller that collects it
    lazily (for example, only for the sessions where some instrument lacks bars)
    gets an exact ``unexplained_bar_gap`` verdict but partial
    ``suspension_bar_conflict`` coverage; passing ``None`` means "not checked"
    and disables both rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Collection, Mapping, Sequence

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
    severity: str = "blocking"

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "instruments": list(self.instruments),
            "severity": self.severity,
        }


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
        return "passed" if not [item for item in self.findings if item.severity == "blocking"] else "blocked"

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
    suspensions: Mapping[str, Sequence[date]] | None = None,
    suspension_provenance: str = "unspecified",
    delisted: Collection[str] | None = None,
    delisted_provenance: str = "unspecified",
    factor_sessions: Sequence[date] | None = None,
) -> ReadinessReport:
    """Evaluate the cross-input conditions a training run depends on."""
    session_values = sorted(set(sessions))
    findings: list[ReadinessFinding] = []
    metrics: dict[str, float] = {}
    suspended_by_instrument = {
        instrument: set(values) for instrument, values in (suspensions or {}).items()
    }

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
    # With a suspension source available the per-session rules below give the
    # explained/unexplained verdict, so the blunt member-level finding would
    # only double-report a halt as a defect.
    if members_without_bars and not frame.empty and suspensions is None:
        findings.append(
            ReadinessFinding(
                "member_without_bars",
                f"{len(members_without_bars)} universe members have no bars in the window",
                tuple(members_without_bars[:50]),
            )
        )

    present_sessions = sessions_by_instrument(frame)
    observable_counts: dict[str, int] = {}
    for instrument in set(members) | set(with_bars):
        suspended = suspended_by_instrument.get(instrument, set())
        observable = [value for value in session_values if value not in suspended]
        observable_counts[instrument] = len(observable)
    fully_suspended = sorted(
        instrument for instrument in members if observable_counts.get(instrument) == 0
    )
    if fully_suspended:
        metrics["instruments_fully_suspended"] = float(len(fully_suspended))
    if suspensions is not None and session_values:
        # A gap is only actionable once the governed suspension source says the
        # session was not tradable: otherwise a mid-window ingest hole for one
        # name is indistinguishable from a two-week restructuring halt.
        gaps: list[str] = []
        conflicts: list[str] = []
        excused_delisted: list[str] = []
        for instrument in sorted(set(members) | set(with_bars)):
            present = present_sessions.get(instrument, set())
            suspended = suspended_by_instrument.get(instrument, set())
            if suspended & present:
                conflicts.append(instrument)
            missing = {value for value in session_values if value not in suspended} - present
            if not missing:
                continue
            # A provider-confirmed delisting excuses an absence that is total for
            # the window. It cannot excuse a partial one: the account level offers
            # no delisting date, so only "never traded at all here" is verifiable,
            # and a mid-window ingest hole keeps its own signature.
            if delisted is not None and not present and instrument in delisted:
                excused_delisted.append(instrument)
                continue
            gaps.append(instrument)
        if excused_delisted:
            metrics["instruments_delisted_excluded"] = float(len(excused_delisted))
            findings.append(
                ReadinessFinding(
                    "delisted_members_excluded",
                    f"{len(excused_delisted)} members have no bars in the window and "
                    f"{delisted_provenance} marks them delisted",
                    tuple(excused_delisted[:50]),
                    "informational",
                )
            )
        if gaps:
            findings.append(
                ReadinessFinding(
                    "unexplained_bar_gap",
                    f"{len(gaps)} instruments lack sessions that {suspension_provenance} does not "
                    "attribute to a suspension",
                    tuple(gaps[:50]),
                )
            )
        if conflicts:
            findings.append(
                ReadinessFinding(
                    "suspension_bar_conflict",
                    f"{len(conflicts)} instruments have bars on sessions the suspension source marks "
                    f"suspended ({suspension_provenance})",
                    tuple(conflicts[:50]),
                )
            )

    short = sorted(
        instrument
        for instrument, count in rows_by_instrument.items()
        if count < min_sessions_per_instrument
    )
    if short:
        # Distinguish "the window never offered enough tradable sessions" (halt
        # or recent listing -> widen the window) from "sessions were tradable
        # but the bars are not there" (an ingest defect to repair).
        limited = (
            [instrument for instrument in short if observable_counts.get(instrument, 0) < min_sessions_per_instrument]
            if suspensions is not None
            else []
        )
        missing = [instrument for instrument in short if instrument not in limited]
        for code, group in (("insufficient_history", missing), ("limited_tradable_history", limited)):
            if not group:
                continue
            body = f"{len(group)} instruments have fewer than {min_sessions_per_instrument} bar sessions"
            if suspensions is not None:
                body += ";tradable " + ",".join(
                    f"{instrument}={rows_by_instrument[instrument]}/{observable_counts.get(instrument, 0)}"
                    for instrument in group[:10]
                )
            findings.append(ReadinessFinding(code, body, tuple(group[:50])))

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
    if factor_sessions is not None:
        findings.extend(
            assess_factor_label_linkage(
                window_start=window_start,
                window_end=window_end,
                sessions=session_values,
                label_horizon_sessions=label_horizon_sessions,
                factor_sessions=factor_sessions,
            )
        )
        factor_values = sorted(set(factor_sessions))
        metrics["factor_sessions"] = float(len(factor_values))
        if usable_label_end is not None:
            metrics["label_only_sessions"] = float(
                len([value for value in session_values if value > usable_label_end])
            )
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


def assess_factor_label_linkage(
    *,
    window_start: date,
    window_end: date,
    sessions: Sequence[date],
    label_horizon_sessions: int,
    factor_sessions: Sequence[date],
) -> tuple[ReadinessFinding, ...]:
    """Assert the feature side of the sample never reaches past the label side.

    A decision date ``t`` is trainable only when bars exist through ``t +
    label_horizon``, so the last trainable decision date is
    ``usable_label_end``. The factor partitions are the feature side of that
    sample: partitions past that date describe decision dates whose labels
    cannot be completed, and partitions that start late silently shrink the
    first trainable dates. Both are fail-closed here because neither shows up
    as a data defect anywhere else.

    When the window itself cannot satisfy the horizon the horizon rule already
    reports it, so this returns nothing rather than double-reporting.
    """
    session_values = sorted(set(sessions))
    usable_label_end = _usable_label_end(session_values, label_horizon_sessions)
    if usable_label_end is None:
        return ()
    factor_values = sorted(set(factor_sessions))
    if not factor_values:
        return (
            ReadinessFinding(
                "no_factor_sessions",
                "the factor layer declares no sessions for this window, so the run would "
                "train on nothing",
            ),
        )
    findings: list[ReadinessFinding] = []
    outside = [value for value in factor_values if value < window_start or value > window_end]
    if outside:
        findings.append(
            ReadinessFinding(
                "factor_session_outside_window",
                f"{len(outside)} factor sessions fall outside {window_start}..{window_end}: "
                + ",".join(value.isoformat() for value in outside[:10]),
            )
        )
    # The window can begin on a weekend/holiday; requiring a factor session on
    # that literal date would mis-block month-start windows.
    in_window = [value for value in session_values if value >= window_start]
    if in_window and factor_values[0] > in_window[0]:
        findings.append(
            ReadinessFinding(
                "factor_starts_after_window",
                f"the factor layer starts at {factor_values[0]} after the first session "
                f"{in_window[0]} in the window, so the earliest decision dates have no features",
            )
        )
    beyond = [value for value in factor_values if value > usable_label_end]
    if beyond:
        findings.append(
            ReadinessFinding(
                "factor_reads_past_label_end",
                f"{len(beyond)} factor sessions fall after {usable_label_end}, the last decision "
                f"date whose {label_horizon_sessions}-session label is complete "
                f"(first: {beyond[0]}); those samples cannot be labeled",
            )
        )
    return tuple(findings)


def sessions_by_instrument(frame: pd.DataFrame) -> dict[str, set[date]]:
    """Sessions each instrument actually has bars for, as plain dates."""
    if frame.empty or "session_date" not in frame.columns:
        return {}
    sessions = pd.to_datetime(frame["session_date"]).dt.date
    grouped: dict[str, set[date]] = {}
    for instrument, value in zip(frame["instrument"], sessions):
        grouped.setdefault(str(instrument), set()).add(value)
    return grouped
