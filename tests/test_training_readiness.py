"""Training readiness rules (pendding item 5): cross-input fail-closed checks."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from uq.pipeline.readiness import assess_factor_label_linkage, assess_readiness

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "validate_training_readiness", _ROOT / "scripts" / "validate_training_readiness.py"
)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

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


def test_a_fully_suspended_member_is_explained_rather_than_reported_as_a_gap():
    bars = _bars(instruments=["600000.XSHG"])
    report = _assess(bars, _members(), suspensions={"000001.XSHE": SESSIONS})
    assert report.status == "passed", report.findings
    assert report.metrics["instruments_fully_suspended"] == 1.0


def test_a_delisted_member_without_any_bars_is_reported_but_not_blocking():
    bars = _bars(instruments=["600000.XSHG"])
    report = _assess(bars, _members(), suspensions={}, delisted={"000001.XSHE"})
    assert report.status == "passed", report.findings
    assert report.metrics["instruments_delisted_excluded"] == 1.0
    finding = report.findings[0]
    assert finding.code == "delisted_members_excluded"
    assert finding.severity == "informational"
    assert report.to_json()["findings"][0]["severity"] == "informational"


def test_a_delisted_member_with_a_partial_gap_is_still_a_blocking_gap():
    bars = pd.concat(
        [
            _bars(instruments=["600000.XSHG"]),
            _bars(instruments=["000001.XSHE"], sessions=SESSIONS[:-1]),
        ],
        ignore_index=True,
    )
    report = _assess(bars, _members(), suspensions={}, delisted={"000001.XSHE"})
    # a delisting flag without a date cannot turn a mid-window hole into an
    # explained absence, even for a name the provider says is gone
    gap = next(item for item in report.findings if item.code == "unexplained_bar_gap")
    assert gap.severity == "blocking"
    assert gap.instruments == ("000001.XSHE",)
    assert report.status == "blocked"


def test_a_gap_the_suspension_source_cannot_explain_blocks():
    bars = _bars(
        mutate=lambda instrument, session: {"close": None} if session == SESSIONS[4] else {}
    )
    bars = bars[bars["close"].notna()].reset_index(drop=True)
    report = _assess(bars, _members(), suspensions={"600000.XSHG": SESSIONS[:1]})
    finding = next(item for item in report.findings if item.code == "unexplained_bar_gap")
    assert set(finding.instruments) == {"600000.XSHG", "000001.XSHE"}


def test_bars_on_a_suspended_session_are_reported_as_a_conflict():
    report = _assess(_bars(), _members(), suspensions={"600000.XSHG": SESSIONS[3:4]})
    assert [item.code for item in report.findings] == ["suspension_bar_conflict"]


def test_tradable_history_below_the_minimum_is_a_distinct_verdict():
    halted = SESSIONS[15:]
    bars = _bars(instruments=["600000.XSHG"], sessions=SESSIONS[:15])
    report = _assess(
        bars,
        {session: ["600000.XSHG"] for session in SESSIONS},
        suspensions={"600000.XSHG": halted},
    )
    codes = [item.code for item in report.findings]
    assert "limited_tradable_history" in codes
    assert "insufficient_history" not in codes
    assert "unexplained_bar_gap" not in codes
    detail = next(item.detail for item in report.findings if item.code == "limited_tradable_history")
    assert "600000.XSHG=15/15" in detail


def test_sessions_outside_the_requested_window_block():
    report = _assess(_bars(), _members(), window_start=SESSIONS[2], window_end=SESSIONS[-3])
    assert any(item.code == "session_outside_window" for item in report.findings)


def test_a_window_ending_on_a_non_trading_day_still_passes():
    # Month ends are usually holidays: the effective end is the last session,
    # so the window boundary must not be treated as a missing session.
    report = _assess(_bars(), _members(), window_end=SESSIONS[-1] + timedelta(days=2))
    assert report.status == "passed", report.findings


def _linkage(factor_sessions, **overrides):
    options = {
        "window_start": SESSIONS[0],
        "window_end": SESSIONS[-1],
        "sessions": SESSIONS,
        "label_horizon_sessions": 5,
        "factor_sessions": factor_sessions,
    }
    options.update(overrides)
    return assess_factor_label_linkage(**options)


def test_linkage_accepts_a_factor_layer_that_stops_at_the_label_end():
    # Features for every decision date whose 5-session label fits inside the
    # window: the sample index is exactly [SESSIONS[0], usable_label_end].
    usable = SESSIONS[-6]
    assert _linkage([day for day in SESSIONS if day <= usable]) == ()


def test_linkage_blocks_factor_sessions_past_the_label_end():
    findings = _linkage(SESSIONS)
    assert [item.code for item in findings] == ["factor_reads_past_label_end"]
    # the tail is exactly the sessions whose labels cannot be completed
    assert SESSIONS[-6].isoformat() in findings[0].detail
    assert SESSIONS[-5].isoformat() in findings[0].detail


def test_linkage_blocks_a_late_factor_start():
    # Warmup that silently drops the first decision dates is sample shrinkage,
    # not a data defect anywhere else.
    findings = _linkage([day for day in SESSIONS[1:] if day <= SESSIONS[-6]])
    assert [item.code for item in findings] == ["factor_starts_after_window"]
    assert SESSIONS[1].isoformat() in findings[0].detail


def test_linkage_allows_a_month_start_window():
    # The requested window may begin on a holiday; the factor layer only needs
    # to cover the first trading session in the window.
    assert (
        _linkage(
            [day for day in SESSIONS if day <= SESSIONS[-6]],
            window_start=SESSIONS[0] - timedelta(days=1),
            window_end=SESSIONS[-1],
        )
        == ()
    )


def test_linkage_blocks_sessions_outside_the_window():
    findings = _linkage([SESSIONS[0] - timedelta(days=1)] + [day for day in SESSIONS if day <= SESSIONS[-6]])
    assert [item.code for item in findings] == ["factor_session_outside_window"]
    # a future session trips both rules, which is expected: it is outside the
    # window *and* past the last labelable decision date
    both = _linkage([SESSIONS[0], date(2027, 1, 1)])
    assert {item.code for item in both} == {"factor_session_outside_window", "factor_reads_past_label_end"}


def test_linkage_stays_silent_when_the_horizon_rule_already_fires():
    # a window too short for the horizon is already reported by the horizon
    # rule, so the linkage layer stays silent instead of double-reporting
    assert _linkage(SESSIONS[:4]) == ()


def test_linkage_blocks_an_empty_factor_layer():
    findings = _linkage([])
    assert [item.code for item in findings] == ["no_factor_sessions"]


def test_readiness_reports_the_linkage_and_the_label_only_tail():
    # SESSIONS[-5:] carry bars purely so the labels of the decision dates can be
    # resolved; that is the boundary the linkage rule protects.
    report = _assess(
        _bars(),
        _members(),
        factor_sessions=[day for day in SESSIONS if day <= SESSIONS[-6]],
    )
    assert report.status == "passed", report.findings
    assert report.metrics["factor_sessions"] == float(len(SESSIONS) - 5)
    assert report.metrics["label_only_sessions"] == 5.0

    blocked = _assess(_bars(), _members(), factor_sessions=SESSIONS)
    assert blocked.status == "blocked"
    assert "factor_reads_past_label_end" in {item.code for item in blocked.findings}


def test_script_accepts_binding_partitions_and_bare_dates(tmp_path):
    binding = tmp_path / "factor-set.json"
    binding.write_text(
        '{"partitions": [{"partition_date": "2026-01-05"}, {"partition_date": "2026-01-06"}]}',
        encoding="utf-8",
    )
    assert script.factor_sessions_from_document(script._read_factor_partition_set(binding)) == [
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]
    assert script.factor_sessions_from_document(["2026-01-06", "2026-01-05", "2026-01-05"]) == [
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]


def test_script_rejects_malformed_partition_sets(tmp_path):
    with pytest.raises(SystemExit, match="unreadable factor partition set"):
        script._read_factor_partition_set(tmp_path / "missing.json")
    with pytest.raises(SystemExit, match="non-empty"):
        script.factor_sessions_from_document({"partitions": []})
    with pytest.raises(SystemExit, match="no date"):
        script.factor_sessions_from_document([{"other": 1}])
    with pytest.raises(SystemExit, match="not ISO"):
        script.factor_sessions_from_document(["2026/01/05"])
