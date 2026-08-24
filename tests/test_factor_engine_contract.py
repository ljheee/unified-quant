import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
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


def test_future_session_fails_closed_before_planning():
    with pytest.raises(ContractError, match="beyond visible cutoff"):
        engine().build_request(
            trade_date=date(2026, 8, 21), factor_set="basic", factor_version="1.0.0",
            session_dates=(date(2026, 8, 22),),
        )


def test_factor_result_empty_all_null_and_partial_semantics():
    request = engine().build_request(
        trade_date=date(2026, 8, 21), factor_set="basic", factor_version="1.0.0"
    )
    columns = ["instrument", "datetime", *[item["name"] for item in request.definition.factors]]
    empty = engine().create_result(request, pd.DataFrame(columns=columns))
    assert empty.status == "empty"
    assert empty.null_policy["staging_only"] is True

    all_null = pd.DataFrame([{
        "instrument": "A", "datetime": pd.Timestamp(2026, 8, 21),
        **{name: None for name in [item["name"] for item in request.definition.factors]},
    }])
    rejected = engine().create_result(request, all_null)
    assert rejected.status == "rejected"
    assert rejected.errors

    factor_names = [item["name"] for item in request.definition.factors]
    rows = [
        {"instrument": "A", "datetime": timestamp, **{name: (None if name == "volume_ratio_20d" and index == 0 else 1.0 if index else 0.0) for name in factor_names}}
        for index, timestamp in enumerate(pd.to_datetime(["2026-08-20", "2026-08-21"]))
    ]
    partial = pd.DataFrame(rows)
    passed = engine().create_result(request, partial)
    assert passed.status == "passed"
    assert passed.warnings


def test_coverage_below_minimum_rejects_result():
    request = engine().build_request(
        trade_date=date(2026, 8, 21), factor_set="basic", factor_version="1.0.0",
        session_dates=(date(2026, 8, 20), date(2026, 8, 21)),
    )
    import copy
    from uq.contracts.factor_governance import FactorSetDefinition
    document = copy.deepcopy(request.definition.document)
    document["quality_thresholds"]["coverage_minimum"] = 0.75
    definition = FactorSetDefinition(document)
    names = [item["name"] for item in request.definition.factors]
    rows = [
        {"instrument": "A", "datetime": timestamp, **{name: None for name in names}}
        for timestamp in pd.to_datetime(["2026-08-20", "2026-08-21"])
    ]
    rows[1] = {
        "instrument": "A", "datetime": rows[1]["datetime"],
        **{name: 1.0 for name in names},
    }
    from dataclasses import replace
    governed_request = replace(request, definition=definition)
    result = engine().create_result(governed_request, pd.DataFrame(rows))
    assert result.status == "rejected"
    assert "coverage below minimum" in result.errors


def test_external_quality_report_must_match_local_decision():
    request = engine().build_request(
        trade_date=date(2026, 8, 21), factor_set="basic", factor_version="1.0.0"
    )
    frame = pd.DataFrame([{
        "instrument": "A", "datetime": pd.Timestamp(2026, 8, 21),
        **{name: 1.0 for name in [item["name"] for item in request.definition.factors]},
    }])
    passing = engine().create_result(request, frame).quality_report

    mismatched_status = {**passing, "policy": "reject_all", "status": "rejected"}
    with pytest.raises(ContractError, match="quality status disagrees"):
        engine().create_result(request, frame, quality_report=mismatched_status)

    mismatched_checks = {
        **passing,
        "checks": [
            *passing["checks"][:-1],
            {**passing["checks"][-1], "result": "failed"},
        ],
    }
    with pytest.raises(ContractError, match="quality checks disagree"):
        engine().create_result(request, frame, quality_report=mismatched_checks)


def test_publication_intent_rejects_empty_result():
    request = engine().build_request(
        trade_date=date(2026, 8, 21), factor_set="basic", factor_version="1.0.0",
        intent="publication",
    )
    with pytest.raises(ContractError):
        engine().create_result(request, pd.DataFrame(columns=["instrument", "datetime", "amount_20d"]))
