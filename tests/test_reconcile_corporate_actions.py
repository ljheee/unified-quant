"""Regression tests for scripts/reconcile_corporate_actions.py.

The direct event-to-event comparison is only useful if it runs from the same
announcement-anchored store the derivation consumes, keeps amount and coverage
divergences apart, and reports a share-only disclosure as uncovered instead of
refusing the whole instrument over a record the *comparison* can still express.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pandas as pd

from uq.contracts.dividend_events import DividendEventStore

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_corporate_actions", _ROOT / "scripts" / "reconcile_corporate_actions.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

RAW_COLUMNS = [
    "ts_code", "end_date", "ann_date", "div_proc", "stk_div", "stk_bo_rate",
    "stk_co_rate", "cash_div", "cash_div_tax", "record_date", "ex_date",
    "pay_date", "div_listdate", "imp_ann_date",
]
TDX_COLUMNS = ["year", "month", "day", "category", "fenhong", "songzhuangu", "peigu", "peigujia"]


def _disclosure_row(**overrides):
    row = {
        "ts_code": "600000.SH", "end_date": "20250630", "ann_date": "20250829",
        "div_proc": "实施", "stk_div": 0.0, "stk_bo_rate": None, "stk_co_rate": None,
        "cash_div": None, "cash_div_tax": 0.41, "record_date": None,
        "ex_date": "20250916", "pay_date": None, "div_listdate": None,
        "imp_ann_date": "20250910",
    }
    row.update(overrides)
    return row


def _publish_disclosures(root: Path, *rows):
    # A record may only be published under its own announcement date, and both its
    # ex-date and its implementation date must fall on or after that announcement.
    store = DividendEventStore(root)
    frame = pd.DataFrame(list(rows), columns=RAW_COLUMNS)
    for ann_date, group in frame.groupby("ann_date", sort=True):
        store.publish(
            ann_date=dt.date.fromisoformat(ann_date),
            source="tushare:dividend",
            source_fetched_at=dt.datetime(2025, 9, 1, 9, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            frame=group.reset_index(drop=True),
        )
    return store


def _tdx(*rows):
    return pd.DataFrame(list(rows), columns=TDX_COLUMNS)


def _fake_adapter(frames):
    class _Adapter:
        connected_server = ("test", 7709)

        def fetch_xdxr_events(self, symbols):
            return {symbol: frames.get(symbol, pd.DataFrame(columns=TDX_COLUMNS)) for symbol in symbols}

    return _Adapter()


def _run(tmp_path, monkeypatch, *, instruments, tdx_frames, disclosures, as_of="2026-01-01"):
    root = tmp_path / "dividend"
    _publish_disclosures(root, *disclosures)
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter(tdx_frames))
    out = tmp_path / "report.json"
    code = module.main([
        "--dividend-root", str(root), "--instruments", *instruments,
        "--as-of", as_of, "--out", str(out),
    ])
    return code, json.loads(out.read_text())


def test_agreement_passes_the_gate(tmp_path, monkeypatch):
    code, report = _run(
        tmp_path, monkeypatch,
        instruments=["600000.XSHG"],
        disclosures=[_disclosure_row()],
        tdx_frames={"600000": _tdx((2025, 9, 16, 1, 4.1, 0.0, 0.0, 0.0))},
    )
    assert code == 0
    assert report["agreed"] == 1
    assert report["amount_diverged"] == 0
    assert report["records"][0]["status"] == "agreed"


def test_an_amount_divergence_fails_the_gate_with_both_values(tmp_path, monkeypatch):
    code, report = _run(
        tmp_path, monkeypatch,
        instruments=["601318.XSHG"],
        disclosures=[_disclosure_row(ts_code="601318.SH", cash_div_tax=0.2, ann_date="20180510",
                                     ex_date="20180607", imp_ann_date="20180601", end_date="20171231")],
        tdx_frames={"601318": _tdx((2018, 6, 7, 1, 12.0, 0.0, 0.0, 0.0))},
    )
    assert code == 1
    assert report["amount_diverged"] == 1
    (record,) = report["records"]
    assert record["status"] == "amount_diverged"
    (divergence,) = record["divergences"]
    assert (divergence["field"], divergence["disclosure"], divergence["tdx"]) == ("fenhong", 2.0, 12.0)


def test_a_coverage_divergence_is_not_folded_into_an_amount_divergence(tmp_path, monkeypatch):
    code, report = _run(
        tmp_path, monkeypatch,
        instruments=["600000.XSHG"],
        disclosures=[_disclosure_row()],
        tdx_frames={"600000": _tdx(
            (2025, 9, 16, 1, 4.1, 0.0, 0.0, 0.0),
            (2025, 12, 1, 1, 1.0, 0.0, 0.0, 0.0),
        )},
    )
    assert code == 1
    assert report["amount_diverged"] == 0
    assert report["coverage_diverged"] == 1
    assert report["records"][0]["tdx_only_dates"] == ["2025-12-01"]


def test_a_share_only_disclosure_is_uncovered_not_a_refusal_of_the_name(tmp_path, monkeypatch):
    # The derivation fails closed on a share-only distribution (it has no cash to
    # derive a factor from), but the *comparison* can still express the event, so it
    # is compared rather than dropping the instrument from the gate.
    code, report = _run(
        tmp_path, monkeypatch,
        instruments=["600000.XSHG"],
        disclosures=[
            _disclosure_row(),
            _disclosure_row(end_date="20241231", cash_div_tax=None, stk_div=1.0,
                            stk_bo_rate=1.0, stk_co_rate=0.0, ex_date="20251201",
                            imp_ann_date="20250901"),
        ],
        tdx_frames={"600000": _tdx((2025, 9, 16, 1, 4.1, 0.0, 0.0, 0.0))},
    )
    assert code == 0
    assert report["instruments_compared"] == 1
    assert report["records"][0]["status"] == "agreed"
    assert report["refusals"] == []
    (note,) = report["uncovered"]
    assert note["side"] == "disclosure"
    assert note["excluded_ex_dates"] == ["2025-12-01"]


def test_a_name_with_only_a_share_only_disclosure_is_reported_as_uncovered(tmp_path, monkeypatch):
    code, report = _run(
        tmp_path, monkeypatch,
        instruments=["600000.XSHG"],
        disclosures=[_disclosure_row(cash_div_tax=None, stk_div=1.0, stk_bo_rate=1.0,
                                     stk_co_rate=0.0, ex_date="20251201", imp_ann_date="20250901")],
        tdx_frames={"600000": _tdx((2025, 12, 1, 1, 0.0, 10.0, 0.0, 0.0))},
    )
    assert code == 1
    assert report["instruments_compared"] == 0
    assert "no pre-tax cash field" in report["refusals"][0]["reason"]


def test_a_genuinely_unmappable_disclosure_is_a_refusal_not_a_traceback(tmp_path, monkeypatch):
    # The reduced pass (cash-bearing records only) can still fail -- here a record
    # whose implementation date is after its own ex-date. That is dirty data, not a
    # share-only exclusion, so it must be a reported refusal (gate failure) rather
    # than an unhandled ContractError aborting the run.
    code, report = _run(
        tmp_path, monkeypatch,
        instruments=["600000.XSHG"],
        disclosures=[
            _disclosure_row(cash_div_tax=None, stk_div=1.0, stk_bo_rate=1.0,
                            stk_co_rate=0.0, ex_date="20251201", imp_ann_date="20250901"),
            _disclosure_row(ex_date="20250916", imp_ann_date="20250920"),
        ],
        tdx_frames={"600000": _tdx((2025, 9, 16, 1, 4.1, 0.0, 0.0, 0.0))},
    )
    assert code == 1
    assert report["instruments_compared"] == 0
    assert "announced after its own ex-date" in report["refusals"][0]["reason"]
