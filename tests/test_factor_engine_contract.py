import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError
from uq.factors.engine import FactorEngine, WindowSelector


ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)


def engine():
    return FactorEngine(ROOT, FactorRegistry(ROOT), run_visible_cutoff=CUTOFF)


def test_facade_and_expanded_request_compile_identically():
    facade = engine().compute(date(2026, 8, 21), "basic", "1.0.0")
    expanded = engine().build_request(
        trade_date=date(2026, 8, 21), factor_set="basic", factor_version="1.0.0",
        session_dates=(date(2026, 8, 21),),
        decision_time=datetime(2026, 8, 21, 15, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai")),
    )
    assert facade.execution_plan == expanded.execution_plan
    assert facade.request_metadata["facade"] is True
    assert expanded.request_metadata["facade"] is False


def test_window_and_universe_binding_plan():
    universe = {
        "generation_id": "a" * 64,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "b" * 64},
        "valid_from": "2026-08-01", "valid_to": None,
    }
    request = engine().build_request(
        trade_date=date(2026, 8, 21), factor_set="basic", factor_version="1.0.0",
        window=WindowSelector(date(2026, 7, 1), date(2026, 8, 21)), universe_binding=universe,
    )
    assert request.execution_plan["payload"]["universe_generation_id"] == "a" * 64


@pytest.mark.parametrize("kwargs,message", [
    ({"session_dates": (date(2026, 8, 22), date(2026, 8, 21))}, "strictly ordered"),
    ({"session_dates": ()}, "non-empty"),
    ({"run_visible_cutoff": datetime(2026, 8, 20, tzinfo=timezone.utc)}, "cutoff precedes"),
    ({"factor_set": "unknown"}, "unknown factor set/version"),
])
def test_typed_invalid_requests(kwargs, message):
    base = {"trade_date": date(2026, 8, 21), "factor_set": "basic", "factor_version": "1.0.0"}
    with pytest.raises(ContractError, match=message):
        engine().build_request(**{**base, **kwargs})


def test_invalid_universe_and_ambiguous_selector():
    with pytest.raises(ContractError, match="invalid factor universe binding"):
        engine().build_request(
            trade_date=date(2026,8,21), factor_set="basic", factor_version="1.0.0",
            universe_binding={"bad": True})
    with pytest.raises(ContractError, match="cannot supply both"):
        engine().build_request(
            trade_date=date(2026,8,21), factor_set="basic", factor_version="1.0.0",
            session_dates=(date(2026,8,21),), window=WindowSelector(date(2026,8,20),date(2026,8,21)))
