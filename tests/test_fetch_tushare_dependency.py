import importlib.util
from pathlib import Path

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
        "daily_basic",
        "index_member_all",
        "income",
        "report_rc",
        "moneyflow",
        "hk_hold",
        "top_list",
    } <= _MODULE.TUSHARE_RESEARCH_ENDPOINTS
