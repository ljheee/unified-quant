import importlib.util
from pathlib import Path

import pandas as pd


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_tushare_industry_history.py"
_SPEC = importlib.util.spec_from_file_location("fetch_tushare_industry_history", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class _Adapter:
    def __init__(self):
        self.queries = []

    def research_data(self, endpoint, **query):
        self.queries.append((endpoint, query))
        if endpoint == "index_classify":
            return pd.DataFrame({
                "index_code": ["850101.SI", "850102.SI"],
                "industry_name": ["A", "B"],
                "level": ["L3", "L3"],
            })
        return pd.DataFrame({
            "ts_code": ["600000.SH"],
            "l1_code": ["801010.SI"],
            "l2_code": ["801011.SI"],
            "l3_code": [query["l3_code"]],
            "in_date": ["20200101"],
            "out_date": [""],
        })


def test_industry_fetch_stores_catalog_and_per_code_history(tmp_path, monkeypatch):
    adapter = _Adapter()
    monkeypatch.setattr(_MODULE, "Tushare2kAdapter", lambda: adapter)
    summary = tmp_path / "industry-summary.json"

    status = _MODULE.main([
        "--store-root", str(tmp_path / "store"),
        "--max-requests", "5",
        "--request-interval-seconds", "0",
        "--summary-out", str(summary),
    ])

    assert status == 0
    assert adapter.queries == [
        ("index_classify", {"level": "L3", "src": "SW2021"}),
        ("index_member_all", {"l3_code": "850101.SI", "is_new": "Y"}),
        ("index_member_all", {"l3_code": "850101.SI", "is_new": "N"}),
        ("index_member_all", {"l3_code": "850102.SI", "is_new": "Y"}),
        ("index_member_all", {"l3_code": "850102.SI", "is_new": "N"}),
    ]
    assert '"industry_count": 2' in summary.read_text(encoding="utf-8")
