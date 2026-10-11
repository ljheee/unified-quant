import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "probe_reference_data_coverage", _ROOT / "scripts" / "probe_reference_data_coverage.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def test_normalize_maps_every_supported_exchange_including_beijing():
    raw = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ", "920438.BJ"], "adj_factor": [1.0, 2.0, 3.0]})
    normalized = module.normalize(raw, label="adj_factor")
    assert normalized["instrument"].tolist() == ["600000.XSHG", "000001.XSHE", "920438.XBSE"]


def test_normalize_rejects_unsupported_exchange_fail_closed():
    raw = pd.DataFrame({"ts_code": ["0700.HK"], "adj_factor": [1.0]})
    with pytest.raises(ContractError, match="unsupported Tushare codes"):
        module.normalize(raw, label="adj_factor")


def test_normalize_keeps_empty_frames_typed():
    normalized = module.normalize(pd.DataFrame({"ts_code": []}), label="adj_factor")
    assert normalized.empty
    assert "instrument" in normalized.columns


def test_coverage_flags_missing_members_and_invalid_factors():
    universe = {"600000.XSHG", "000001.XSHE", "600519.XSHG"}
    frames = {
        "adj_factor": pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ"],
            "instrument": ["600000.XSHG", "000001.XSHE"],
            "adj_factor": [1.0, 0.0],
        }),
        "suspension": pd.DataFrame({
            "ts_code": ["600519.SH"],
            "instrument": ["600519.XSHG"],
        }),
    }
    report = module.dataset_coverage(universe, frames)
    assert report["adj_factor"]["coverage"] == pytest.approx(2 / 3)
    assert report["adj_factor"]["adj_factor_nonpositive"] == 1
    assert report["adj_factor"]["adj_factor_null"] == 0
    assert report["suspension"]["suspended_in_universe"] == 1


def test_coverage_of_an_empty_universe_is_not_a_division_error():
    report = module.dataset_coverage(set(), {
        "adj_factor": pd.DataFrame({"ts_code": [], "instrument": []}),
    })
    assert report["adj_factor"]["coverage"] is None
    assert report["adj_factor"]["rows"] == 0
