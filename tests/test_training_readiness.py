"""Training readiness rules (pendding item 5): cross-input fail-closed checks."""

from datetime import date, timedelta

import pandas as pd

from uq.pipeline.readiness import assess_readiness

SESSIONS = [date(2026, 1, 1) + timedelta(days=offset) for offset in range(0, 40)]
INSTRUMENTS = ["600000.XSHG", "000001.XSHE"]


def _bars(instruments=INSTRUMENTS, sessions=SESSIONS, mutate=None) -> pd.DataFrame:
    rows = []
    for instrument in instruments:
        for session in sessions:
            row = {
                "instrument": instrument,
                "session_date": pd.Timestamp(session),
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1000.0,
                "amount": 10200.0,
                "status": "trading",
                "adj_factor": 1.5,
                "limit_up": 11.22,
                "limit_down": 9.18,
            }
            if mutate is not None:
                row.update(mutate(instrument, session))
            rows.append(row)
    return pd.DataFrame(rows)


def _members(instruments=INSTRUMENTS) -> dict:
    return {session: list(instruments) for session in SESSIONS}


def _assess(bars: pd.DataFrame, members: dict, **overrides):
    options = {
        "window_start": SESSIONS[0],
        "window_end": SESSIONS[-1],
        "sessions": SESSIONS,
        "members_by_session": members,
        "bars": bars,
        "min_sessions_per_instrument": 20,
        "label_horizon_sessions": 5,
    }
    options.update(overrides)
    return assess_readiness(**options)


def test_healthy_panel_passes_and_reports_the_usable_label_end():
    report = _assess(_bars(), _members())
    assert report.status == "passed", report.findings
    assert report.sessions == len(SESSIONS)
    assert report.members == 2
    # the 5-session forward label must fit inside the observed sessions
    assert report.usable_label_end == SESSIONS[-6]
    payload = report.to_json()
    assert payload["status"] == "passed"
    assert payload["usable_label_end"] == SESSIONS[-6].isoformat()


def test_universe_member_without_bars_blocks():
    bars = _bars(instruments=["600000.XSHG"])
    report = _assess(bars, _members())
    assert report.status == "blocked"
    finding = next(item for item in report.findings if item.code == "member_without_bars")
    assert finding.instruments == ("000001.XSHE",)


def test_short_history_blocks_with_the_offending_instruments():
    bars = pd.concat(
        [
            _bars(instruments=["600000.XSHG"]),
            _bars(instruments=["000001.XSHE"], sessions=SESSIONS[:5]),
        ],
        ignore_index=True,
    )
    report = _assess(bars, _members())
    assert report.status == "blocked"
    assert any(item.code == "insufficient_history" for item in report.findings)
    assert "000001.XSHE" in next(item.instruments for item in report.findings if item.code == "insufficient_history")


def test_session_without_visible_membership_blocks():
    members = _members()
    members.pop(SESSIONS[7])
    report = _assess(_bars(), members)
    assert report.status == "blocked"
    assert any(item.code == "membership_unavailable" for item in report.findings)


def test_non_positive_adjustment_factor_blocks():
    bars = _bars(mutate=lambda instrument, session: {"adj_factor": 0.0 if instrument.endswith("XSHE") else 1.0})
    report = _assess(bars, _members())
    finding = next(item for item in report.findings if item.code == "non_positive_adjustment_factor")
    assert finding.instruments == ("000001.XSHE",)


def test_missing_limit_prices_block_only_on_trading_rows():
    def mutate(instrument, session):
        if session == SESSIONS[3]:
            return {"limit_up": None, "limit_down": None, "volume": 0.0, "status": "suspended"}
        return {"limit_up": None, "limit_down": None} if instrument.endswith("XSHG") else {}

    report = _assess(_bars(mutate=mutate), _members())
    finding = next(item for item in report.findings if item.code == "limit_prices_missing")
    # the suspended row is excluded because no fill could happen on it anyway
    assert finding.instruments == ("600000.XSHG",)
    assert int(finding.detail.split()[0]) == len(SESSIONS) - 1


def test_window_shorter_than_the_label_horizon_blocks():
    short = SESSIONS[:5]
    report = _assess(
        _bars(sessions=short),
        {session: list(INSTRUMENTS) for session in short},
        sessions=short,
        window_start=short[0],
        window_end=short[-1],
    )
    assert report.usable_label_end is None
    assert any(item.code == "label_horizon_unsupported" for item in report.findings)


def test_empty_bars_block_instead_of_passing_vacuously():
    report = _assess(pd.DataFrame(), _members())
    assert report.status == "blocked"
    assert any(item.code == "no_bars" for item in report.findings)


def test_bar_session_gap_against_the_declared_calendar_blocks():
    report = _assess(_bars(sessions=SESSIONS[:-2]), _members())
    assert any(item.code == "bar_session_gap" for item in report.findings)


def test_sessions_outside_the_requested_window_block():
    report = _assess(_bars(), _members(), window_start=SESSIONS[2], window_end=SESSIONS[-3])
    assert any(item.code == "session_outside_window" for item in report.findings)


def test_a_window_ending_on_a_non_trading_day_still_passes():
    # Month ends are usually holidays: the effective end is the last session,
    # so the window boundary must not be treated as a missing session.
    report = _assess(_bars(), _members(), window_end=SESSIONS[-1] + timedelta(days=2))
    assert report.status == "passed", report.findings
