import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_tushare_dependency.py"
_SPEC = importlib.util.spec_from_file_location("fetch_tushare_dependency", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_parse_filters_returns_bounded_scalar_parameters():
    assert _MODULE.parse_filters(["ts_code=600000.SH", "start_date=20200101"]) == {
        "ts_code": "600000.SH",
        "start_date": "20200101",
    }


@pytest.mark.parametrize("items", [["bad"], ["=value"], ["key="], ["key=one", "key=two"]])
def test_parse_filters_rejects_malformed_or_duplicate_filters(items):
    with pytest.raises(ContractError):
        _MODULE.parse_filters(items)


def test_error_redaction_never_emits_tushare_token():
    assert _MODULE._redact("request failed: secret-token", ["secret-token"]) == (
        "request failed: [redacted]"
    )


def test_endpoint_catalog_includes_required_plan_dependencies():
    assert {
        "stock_basic",
        "suspend_d",
        "daily_basic",
        "index_member_all",
        "income",
        "report_rc",
        "moneyflow",
        "hk_hold",
        "top_list",
    } <= _MODULE.TUSHARE_RESEARCH_ENDPOINTS


def test_suspension_range_fetch_pins_daily_suspend_type_and_snapshots(tmp_path, monkeypatch, capsys):
    class RangeAdapter:
        def __init__(self):
            self.queries = []

        def trade_dates(self, start, end):
            return {date(2026, 10, 29), date(2026, 10, 30)}

        def research_data(self, endpoint, **query):
            self.queries.append((endpoint, query))
            return pd.DataFrame({
                "ts_code": ["000861.SZ"],
                "trade_date": [query["trade_date"]],
                "suspend_type": ["S"],
            })

    adapter = RangeAdapter()
    monkeypatch.setattr(_MODULE, "Tushare2kAdapter", lambda: adapter)
    code = _MODULE.main([
        "--endpoint", "suspend_d",
        "--store-root", str(tmp_path / "source"),
        "--range-start", "2026-10-29",
        "--range-end", "2026-10-30",
        "--max-requests", "3",
        "--session-interval-seconds", "0",
        "--summary-out", str(tmp_path / "summary.json"),
    ])

    assert code == 0
    assert [query for endpoint, query in adapter.queries] == [
        {"trade_date": "20261029", "suspend_type": "S"},
        {"trade_date": "20261030", "suspend_type": "S"},
    ]
    report = json.loads((tmp_path / "summary.json").read_text())
    assert report["query_count"] == 2
    assert report["status"] == "published"
    capsys.readouterr()
