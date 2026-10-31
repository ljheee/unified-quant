from __future__ import annotations

import pandas as pd
import pytest

from uq.backtest.corporate_actions import (
    ACTION_EVENT_COLUMNS,
    dividend_coverage_row,
    normalize_action_events,
    rights_issue_coverage_row,
)
from uq.contracts.gate_contracts import sha256_json
from uq.errors import ContractError


DAYS = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]


def _panel(**changes):
    dividend = {
        "event_type": "dividend",
        "event_id": "a" * 64,
        "instrument": "600000.XSHG",
        "record_date": DAYS[1],
        "ex_date": DAYS[2],
        "pay_date": DAYS[3],
        "div_listdate": DAYS[3],
        "cash_div_after_tax": 0.1,
        "stock_div_per_share": 0.5,
        "source_announcement_date": DAYS[0],
        "implementation_announcement_date": DAYS[1],
        "source_record_id": "a" * 64,
        "source_approval_ref": "dividend-review-2025-01",
        "reason": "implemented_dividend",
        "window_start": None,
        "window_end": None,
        "coverage_complete": None,
    }
    dividend.update(changes)
    coverage = rights_issue_coverage_row(
        start_date=DAYS[0], end_date=DAYS[-1], source_approval_ref="rights-list-2025-01",
    )
    dividend_coverage = dividend_coverage_row(
        start_date=DAYS[0], end_date=DAYS[-1], source_approval_ref="dividends-2025-01",
    )
    return pd.DataFrame([dividend, coverage, dividend_coverage], columns=ACTION_EVENT_COLUMNS)


def test_action_panel_binds_record_ex_pay_and_list_sessions():
    schedule = normalize_action_events(
        _panel(), start_date=DAYS[0], end_date=DAYS[-1], trading_dates=DAYS,
    )
    assert [item["event_id"] for item in schedule["record_events"][DAYS[1]]] == ["a" * 64]
    assert [item["event_id"] for item in schedule["ex_events"][DAYS[2]]] == ["a" * 64]
    assert [item["event_id"] for item in schedule["pay_events"][DAYS[3]]] == ["a" * 64]
    assert [item["event_id"] for item in schedule["listing_events"][DAYS[3]]] == ["a" * 64]
    assert schedule["rights_issue_instruments"] == set()


def test_action_panel_keeps_post_window_settlement_as_receivable():
    panel = _panel(pay_date="2025-01-10", div_listdate="2025-01-10")
    schedule = normalize_action_events(
        panel, start_date=DAYS[0], end_date=DAYS[-1], trading_dates=DAYS,
    )
    assert schedule["pay_events"] == {}
    assert schedule["listing_events"] == {}


@pytest.mark.parametrize(("field", "value", "message"), [
    ("record_date", None, "record_date"),
    ("ex_date", "2025-01-05", "inside the backtest window"),
    ("pay_date", None, "pay date"),
    ("div_listdate", None, "listing date"),
    ("source_announcement_date", "2025-01-07", "not visible by ex date"),
    ("cash_div_after_tax", float("nan"), "finite and nonnegative"),
])
def test_action_panel_rejects_incomplete_or_future_disclosure(field, value, message):
    with pytest.raises(ContractError, match=message):
        normalize_action_events(
            _panel(**{field: value}), start_date=DAYS[0], end_date=DAYS[-1],
            trading_dates=DAYS,
        )


def test_action_panel_requires_complete_reviewed_rights_exclusion_coverage():
    panel = _panel()
    rights = rights_issue_coverage_row(
        start_date=DAYS[0], end_date=DAYS[-1], source_approval_ref="rights-list-2025-01",
    )
    rights.update({
        "event_type": "rights_issue_exclusion",
        "event_id": sha256_json({
            "event_type": "rights_issue_exclusion", "instrument": "000001.XSHE",
            "source_approval_ref": "rights-list-2025-01", "reason": "rights_issue_not_modeled",
            "window_start": DAYS[0], "window_end": DAYS[-1],
        }),
        "instrument": "000001.XSHE",
        "source_approval_ref": "rights-list-2025-01",
        "reason": "rights_issue_not_modeled",
    })
    panel = pd.concat([panel, pd.DataFrame([rights], columns=ACTION_EVENT_COLUMNS)], ignore_index=True)
    schedule = normalize_action_events(
        panel, start_date=DAYS[0], end_date=DAYS[-1], trading_dates=DAYS,
    )
    assert schedule["rights_issue_instruments"] == {"000001.XSHE"}

    incomplete = panel.copy()
    coverage_index = incomplete.index[incomplete["event_type"] == "rights_issue_coverage"][0]
    incomplete.loc[coverage_index, "coverage_complete"] = False
    with pytest.raises(ContractError, match="coverage is incomplete"):
        normalize_action_events(
            incomplete, start_date=DAYS[0], end_date=DAYS[-1], trading_dates=DAYS,
        )


def test_action_panel_requires_complete_reviewed_dividend_source_coverage():
    panel = _panel()
    coverage_index = panel.index[panel["event_type"] == "dividend_coverage"][0]
    panel.loc[coverage_index, "coverage_complete"] = False
    with pytest.raises(ContractError, match="dividend-source coverage"):
        normalize_action_events(
            panel, start_date=DAYS[0], end_date=DAYS[-1], trading_dates=DAYS,
        )
