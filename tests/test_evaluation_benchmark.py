from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from uq.contracts.index_membership import IndexMembershipStore
from uq.errors import ContractError
from uq.evaluation.benchmark import (
    BenchmarkWeightStore,
    build_benchmark_weight_partitions,
    read_pit_benchmark_membership,
)


def _members() -> pd.DataFrame:
    return pd.DataFrame([
        {"date": "2025-01-02", "instrument": "A"},
        {"date": "2025-01-02", "instrument": "B"},
        {"date": "2025-01-02", "instrument": "C"},
        {"date": "2025-01-03", "instrument": "A"},
        {"date": "2025-01-03", "instrument": "C"},
    ])


def _publish_membership(store, snapshot_date, instruments, calendar):
    return store.publish(
        universe_id="a500",
        index_code="000510.SH",
        source="offline-reviewed:index_membership",
        source_fetched_at=datetime(2025, 1, 10, tzinfo=timezone(timedelta(hours=8))),
        snapshot_date=snapshot_date,
        publish_lag_days=0,
        trading_days=calendar,
        members=pd.DataFrame({"instrument": instruments}),
    )


def test_pit_membership_reader_binds_daily_members_to_visible_snapshots(tmp_path):
    calendar = [date(2025, 1, day) for day in (2, 3, 6, 7, 8, 9, 10)]
    store = IndexMembershipStore(tmp_path)
    _publish_membership(store, date(2025, 1, 1), ["000001.XSHE", "000002.XSHE"], calendar)
    _publish_membership(store, date(2025, 1, 5), ["000002.XSHE", "600000.XSHG"], calendar)

    members, binding = read_pit_benchmark_membership(
        store,
        universe_id="a500",
        calendar_dates=[day.isoformat() for day in calendar],
        start_date="2025-01-02",
        end_date="2025-01-10",
    )

    assert members.loc[members["date"] == "2025-01-02", "instrument"].tolist() == [
        "000001.XSHE", "000002.XSHE"
    ]
    assert members.loc[members["date"] == "2025-01-06", "instrument"].tolist() == [
        "000002.XSHE", "600000.XSHG"
    ]
    assert binding["index_code"] == "000510.SH"
    assert len(binding["snapshots"]) == 2
    assert {item["snapshot_date"] for item in binding["snapshots"]} == {
        "2025-01-01", "2025-01-05"
    }


def test_pit_membership_reader_rejects_uncovered_window_and_bad_calendar(tmp_path):
    store = IndexMembershipStore(tmp_path)
    calendar = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    _publish_membership(store, date(2025, 1, 1), ["000001.XSHE"], calendar)

    with pytest.raises(ContractError, match="window endpoints"):
        read_pit_benchmark_membership(
            store, universe_id="a500", calendar_dates=[day.isoformat() for day in calendar],
            start_date="2025-01-02", end_date="2025-01-09",
        )
    with pytest.raises(ContractError, match="sorted and unique"):
        read_pit_benchmark_membership(
            store, universe_id="a500", calendar_dates=["2025-01-03", "2025-01-02"],
            start_date="2025-01-02", end_date="2025-01-03",
        )


def test_pit_membership_reader_rejects_no_snapshot_visible_at_window_start(tmp_path):
    calendar = [date(2025, 1, day) for day in (2, 3, 6, 7, 8, 9, 10)]
    store = IndexMembershipStore(tmp_path)
    _publish_membership(store, date(2025, 1, 5), ["000001.XSHE"], calendar)

    with pytest.raises(ContractError, match="no index membership visible"):
        read_pit_benchmark_membership(
            store,
            universe_id="a500",
            calendar_dates=[day.isoformat() for day in calendar],
            start_date="2025-01-02",
            end_date="2025-01-10",
        )


def test_equal_weight_benchmark_uses_daily_pit_membership():
    weights = build_benchmark_weight_partitions(_members(), scheme="equal_weight")

    assert list(weights) == ["2025-01-02", "2025-01-03"]
    assert weights["2025-01-02"].to_dict("records") == [
        {"instrument": "A", "weight": pytest.approx(1 / 3)},
        {"instrument": "B", "weight": pytest.approx(1 / 3)},
        {"instrument": "C", "weight": pytest.approx(1 / 3)},
    ]
    assert weights["2025-01-03"].to_dict("records") == [
        {"instrument": "A", "weight": 0.5},
        {"instrument": "C", "weight": 0.5},
    ]


def test_market_cap_benchmark_normalizes_pit_caps_per_date():
    caps = pd.DataFrame([
        {"date": "2025-01-02", "instrument": "A", "market_cap": 10.0},
        {"date": "2025-01-02", "instrument": "B", "market_cap": 20.0},
        {"date": "2025-01-02", "instrument": "C", "market_cap": 70.0},
        {"date": "2025-01-03", "instrument": "A", "market_cap": 1.0},
        {"date": "2025-01-03", "instrument": "C", "market_cap": 3.0},
    ])

    weights = build_benchmark_weight_partitions(
        _members(), scheme="market_cap_weighted", market_cap=caps
    )

    assert weights["2025-01-02"]["weight"].tolist() == pytest.approx([0.1, 0.2, 0.7])
    assert weights["2025-01-03"]["weight"].tolist() == pytest.approx([0.25, 0.75])


def test_industry_neutral_benchmark_equalizes_industry_budgets():
    industries = pd.DataFrame([
        {"date": "2025-01-02", "instrument": "A", "industry": "technology"},
        {"date": "2025-01-02", "instrument": "B", "industry": "technology"},
        {"date": "2025-01-02", "instrument": "C", "industry": "finance"},
        {"date": "2025-01-03", "instrument": "A", "industry": "technology"},
        {"date": "2025-01-03", "instrument": "C", "industry": "finance"},
    ])

    weights = build_benchmark_weight_partitions(
        _members(), scheme="industry_neutral", industry=industries
    )

    first = weights["2025-01-02"].set_index("instrument")["weight"]
    assert first.to_dict() == pytest.approx({"A": 0.25, "B": 0.25, "C": 0.5})
    assert weights["2025-01-03"]["weight"].tolist() == [0.5, 0.5]


@pytest.mark.parametrize("scheme,kwargs,pattern", [
    ("market_cap_weighted", {}, "requires point-in-time market cap"),
    ("industry_neutral", {}, "requires point-in-time industry"),
    (
        "market_cap_weighted",
        {"market_cap": pd.DataFrame([
            {"date": "2025-01-02", "instrument": "A", "market_cap": 10.0},
            {"date": "2025-01-02", "instrument": "B", "market_cap": 20.0},
        ])},
        "missing point-in-time exposure",
    ),
])
def test_exposure_benchmarks_fail_closed_on_missing_sources_or_members(
    scheme, kwargs, pattern
):
    with pytest.raises(ContractError, match=pattern):
        build_benchmark_weight_partitions(_members(), scheme=scheme, **kwargs)


@pytest.mark.parametrize("market_caps,pattern", [
    ([10.0, 0.0, 70.0, 1.0, 3.0], "finite and positive"),
    ([10.0, float("inf"), 70.0, 1.0, 3.0], "finite and positive"),
    ([10.0, True, 70.0, 1.0, 3.0], "numeric, not boolean"),
])
def test_market_cap_benchmark_rejects_invalid_capitalization_values(market_caps, pattern):
    caps = _members().copy()
    caps["market_cap"] = market_caps
    with pytest.raises(ContractError, match=pattern):
        build_benchmark_weight_partitions(
            _members(), scheme="market_cap_weighted", market_cap=caps
        )


def test_benchmark_rejects_duplicate_members_and_noncanonical_dates():
    duplicate = pd.concat([_members(), _members().iloc[[0]]], ignore_index=True)
    with pytest.raises(ContractError, match="duplicate date/instrument"):
        build_benchmark_weight_partitions(duplicate, scheme="equal_weight")

    malformed = _members()
    malformed.loc[0, "date"] = "2025-1-2"
    with pytest.raises(ContractError, match="invalid session date"):
        build_benchmark_weight_partitions(malformed, scheme="equal_weight")


def test_benchmark_refuses_unknown_weight_scheme():
    with pytest.raises(ContractError, match="unsupported benchmark weighting scheme"):
        build_benchmark_weight_partitions(_members(), scheme="buy_the_dip")


def test_benchmark_weight_store_binds_inputs_and_roundtrips_immutably(tmp_path):
    calendar = [date(2025, 1, 2), date(2025, 1, 3)]
    membership_store = IndexMembershipStore(tmp_path / "membership")
    _publish_membership(
        membership_store, date(2025, 1, 1), ["000001.XSHE", "000002.XSHE"], calendar
    )
    members, membership_set = read_pit_benchmark_membership(
        membership_store,
        universe_id="a500",
        calendar_dates=[day.isoformat() for day in calendar],
        start_date="2025-01-02",
        end_date="2025-01-03",
    )
    weights = build_benchmark_weight_partitions(members, scheme="equal_weight")
    store = BenchmarkWeightStore(tmp_path / "benchmark")
    manifest = store.publish(
        weights,
        scheme="equal_weight",
        universe_id="a500",
        membership_set=membership_set,
        membership_panel=members,
        backtest_config_binding={"generation_id": "1" * 64, "manifest_digest_sha256": "2" * 64},
        strategy_schedule_binding={
            "generation_id": "3" * 64,
            "manifest_digest_sha256": "4" * 64,
            "decision_dates": ["2025-01-02", "2025-01-03"],
        },
        strategy_universe_binding={
            "generation_id": "5" * 64,
            "universe_id": "a500",
            "members_checksum_sha256": "6" * 64,
        },
        terminal_liquidation=False,
        exposure_binding=None,
    )
    restored, restored_weights = store.read(
        manifest["generation_id"], membership_panel=members
    )
    assert restored == manifest
    assert list(restored_weights) == ["2025-01-02", "2025-01-03"]
    pd.testing.assert_frame_equal(
        restored_weights["2025-01-02"], weights["2025-01-02"], check_dtype=False
    )
    assert store.publish(
        weights,
        scheme="equal_weight",
        universe_id="a500",
        membership_set=membership_set,
        membership_panel=members,
        backtest_config_binding={"generation_id": "1" * 64, "manifest_digest_sha256": "2" * 64},
        strategy_schedule_binding={
            "generation_id": "3" * 64,
            "manifest_digest_sha256": "4" * 64,
            "decision_dates": ["2025-01-02", "2025-01-03"],
        },
        strategy_universe_binding={
            "generation_id": "5" * 64,
            "universe_id": "a500",
            "members_checksum_sha256": "6" * 64,
        },
        terminal_liquidation=False,
        exposure_binding=None,
    ) == manifest

    data_path = store.panel_root / f"generation={manifest['generation_id']}" / "weights.parquet"
    frame = pd.read_parquet(data_path)
    frame.loc[0, "weight"] = 0.9
    frame.to_parquet(data_path, index=False)
    with pytest.raises(ContractError, match="data checksum"):
        store.read(manifest["generation_id"])


def test_benchmark_panel_refuses_cap_weighting_without_pit_exposure_binding():
    membership_set = {
        "binding_type": "index_membership_set",
        "universe_id": "a500",
        "index_code": "000510.SH",
        "window_start": "2025-01-02",
        "window_end": "2025-01-02",
        "snapshots": [{"snapshot_date": "2025-01-01"}],
    }
    from uq.contracts.model_layer import sha256_json
    membership_set["set_digest_sha256"] = sha256_json(membership_set)
    weights = {"2025-01-02": pd.DataFrame({"instrument": ["A"], "weight": [1.0]})}
    with pytest.raises(ContractError, match="reviewed PIT exposure binding"):
        BenchmarkWeightStore("unused").publish(
            weights,
            scheme="market_cap_weighted",
            universe_id="a500",
            membership_set=membership_set,
            membership_panel=pd.DataFrame([{"date": "2025-01-02", "instrument": "A"}]),
            backtest_config_binding={"generation_id": "1" * 64, "manifest_digest_sha256": "2" * 64},
            strategy_schedule_binding={
                "generation_id": "3" * 64,
                "manifest_digest_sha256": "4" * 64,
                "decision_dates": ["2025-01-02"],
            },
            strategy_universe_binding={
                "generation_id": "5" * 64,
                "universe_id": "a500",
                "members_checksum_sha256": "6" * 64,
            },
            terminal_liquidation=False,
            exposure_binding=None,
        )


def test_benchmark_weight_store_requires_all_members_on_each_decision_date():
    from uq.contracts.model_layer import sha256_json

    membership_set = {
        "binding_type": "index_membership_set",
        "universe_id": "a500",
        "index_code": "000510.SH",
        "window_start": "2025-01-02",
        "window_end": "2025-01-02",
        "snapshots": [{"snapshot_date": "2025-01-01"}],
    }
    membership_set["set_digest_sha256"] = sha256_json(membership_set)
    with pytest.raises(ContractError, match="exact PIT membership"):
        BenchmarkWeightStore("unused").publish(
            {"2025-01-02": pd.DataFrame({"instrument": ["A"], "weight": [1.0]})},
            scheme="equal_weight",
            universe_id="a500",
            membership_set=membership_set,
            membership_panel=pd.DataFrame([
                {"date": "2025-01-02", "instrument": "A"},
                {"date": "2025-01-02", "instrument": "B"},
            ]),
            backtest_config_binding={"generation_id": "1" * 64, "manifest_digest_sha256": "2" * 64},
            strategy_schedule_binding={
                "generation_id": "3" * 64,
                "manifest_digest_sha256": "4" * 64,
                "decision_dates": ["2025-01-02"],
            },
            strategy_universe_binding={
                "generation_id": "5" * 64,
                "universe_id": "a500",
                "members_checksum_sha256": "6" * 64,
            },
            terminal_liquidation=False,
            exposure_binding=None,
        )


def test_benchmark_backtest_reuses_strategy_schedule_config_and_result_publisher(
    tmp_path, monkeypatch
):
    import uq.evaluation.benchmark_backtest as runner
    from uq.contracts.model_layer import sha256_json
    from tests.test_backtest_v3 import DAYS, NAME, inputs

    data_root = tmp_path / "data"
    config, prices, execution_facts = inputs(data_root)
    days = list(DAYS)
    zero = "0" * 64
    strategy = {
        "contract_version": 3,
        "generation_id": "e" * 64,
        "manifest_digest_sha256": "f" * 64,
        "backtest_config_generation_id": config["generation_id"],
        "target_weight_bindings": [
            {"decision_date": day, "generation_id": zero, "manifest_digest_sha256": zero}
            for day in days
        ],
    }
    monkeypatch.setattr(runner, "_verified_result", lambda *_: (strategy, config, {}))

    class MarketStore:
        def __init__(self, _root):
            pass

        def read_frame(self, _generation, _checksum, *, dataset_type, required_columns):
            if dataset_type == "calendar":
                return pd.DataFrame({"date": days})
            if dataset_type == "suspension":
                return pd.DataFrame(columns=["date", "instrument", "suspended"])
            return pd.DataFrame(columns=["date", "instrument"])

    class UniverseStore:
        def __init__(self, _root):
            pass

        def read_manifest(self, _generation):
            return {
                "universe_id": "a500",
                "members_artifact": {"checksum_sha256": "1" * 64},
            }

        def read_members(self, *_args, **_kwargs):
            return pd.DataFrame({"instrument": [NAME]})

    class RawPrices:
        def __init__(self, _root):
            pass

        def read_window(self, *_args):
            raw = prices.reset_index().rename(columns={"date": "session_date"})
            return config["price_source_binding"], raw

    class FakeResultStore:
        def __init__(self, _root):
            self.manifest = None

        def publish(self, manifest, _frames, *, quality_decision, target_weight_generations):
            assert quality_decision == {"external": "review"}
            assert [item["decision_date"] for item in target_weight_generations] == days
            self.manifest = manifest

        def read(self, _generation):
            return self.manifest, {}

    membership_set = {
        "binding_type": "index_membership_set",
        "universe_id": "a500",
        "index_code": "000510.SH",
        "window_start": config["start_date"],
        "window_end": config["end_date"],
        "snapshots": [{"snapshot_date": "2025-01-01"}],
    }
    membership_set["set_digest_sha256"] = sha256_json(membership_set)
    monkeypatch.setattr(
        runner,
        "read_pit_benchmark_membership",
        lambda *_args, **_kwargs: (
            pd.DataFrame([
                {"date": day, "instrument": NAME}
                for day in days
            ]),
            membership_set,
        ),
    )
    monkeypatch.setattr(runner, "BacktestMarketDatasetStore", MarketStore)
    monkeypatch.setattr(runner, "UniverseSnapshotStore", UniverseStore)
    monkeypatch.setattr(runner, "RawExecutionPriceStore", RawPrices)
    monkeypatch.setattr(runner, "BacktestResultStore", FakeResultStore)

    report = runner.publish_benchmark_backtest(
        data_root=data_root,
        benchmark_root=tmp_path / "benchmark",
        membership_root=tmp_path / "membership",
        strategy_result_generation_id=strategy["generation_id"],
        scheme="equal_weight",
        quality_decision={"external": "review"},
    )
    assert report["scheme"] == "equal_weight"
    assert report["strategy_result_generation_id"] == strategy["generation_id"]
    assert report["decision_dates"] == days
    assert report["terminal_liquidation_attempted"] is False

    import uq.evaluation.walk_forward_continuous as continuous

    class ReceiptStore:
        def __init__(self, _root):
            pass

        def read(self, _generation):
            return {
                "backtest_result_generation_id": strategy["generation_id"],
                "backtest_result_manifest_digest_sha256": strategy["manifest_digest_sha256"],
                "backtest_config_generation_id": config["generation_id"],
                "terminal_liquidation_attempted": True,
            }

    monkeypatch.setattr(continuous, "WalkForwardContinuousStore", ReceiptStore)
    walk_forward_report = runner.publish_benchmark_backtest(
        data_root=data_root,
        benchmark_root=tmp_path / "benchmark",
        membership_root=tmp_path / "membership",
        strategy_result_generation_id=strategy["generation_id"],
        scheme="equal_weight",
        quality_decision={"external": "review"},
        walk_forward_root=tmp_path / "walk-forward",
    )
    assert walk_forward_report["terminal_liquidation_attempted"] is True
    assert walk_forward_report["terminal_liquidation_completed"] is True


def test_v4_benchmark_passes_complete_event_panel_to_common_engine(tmp_path, monkeypatch):
    import uq.evaluation.benchmark_backtest as runner
    from uq.contracts.model_layer import sha256_json
    from tests.test_backtest_v3 import DAYS, NAME
    from uq.backtest.corporate_actions import (
        ACTION_EVENT_COLUMNS, dividend_coverage_row, rights_issue_coverage_row,
    )

    config = {
        "contract_version": 4, "generation_id": "a" * 64,
        "manifest_digest_sha256": "b" * 64, "start_date": DAYS[0], "end_date": DAYS[-1],
        "calendar_binding": {"generation_id": "c" * 64, "checksum_sha256": "d" * 64},
        "universe_binding": {"snapshot_generation_id": "e" * 64},
        "suspension_binding": {"dataset_generation_id": "f" * 64,
                               "data_checksum_sha256": "1" * 64},
        "corporate_action_binding": {"dataset_generation_id": "2" * 64,
                                     "data_checksum_sha256": "3" * 64},
        "price_source_binding": {"family": "bars_daily_v2", "window_start": DAYS[0],
                                  "window_end": DAYS[-1]},
        "execution_facts_binding": {"generation_id": "4" * 64},
    }
    strategy = {
        "contract_version": 4, "generation_id": "5" * 64,
        "manifest_digest_sha256": "6" * 64,
        "backtest_config_generation_id": config["generation_id"],
        "target_weight_bindings": [
            {"decision_date": day, "generation_id": "7" * 64,
             "manifest_digest_sha256": "8" * 64}
            for day in DAYS
        ],
    }
    dividend = {
        "event_type": "dividend", "event_id": "a" * 64, "instrument": NAME,
        "record_date": DAYS[0], "ex_date": DAYS[1], "pay_date": DAYS[2],
        "div_listdate": DAYS[2], "cash_div_after_tax": 0.1,
        "stock_div_per_share": 0.5, "source_announcement_date": DAYS[0],
        "implementation_announcement_date": DAYS[0], "source_record_id": "a" * 64,
        "source_approval_ref": "reviewed-dividend", "reason": "implemented_dividend",
        "window_start": None, "window_end": None, "coverage_complete": None,
    }
    events = pd.DataFrame([
        dividend,
        rights_issue_coverage_row(start_date=DAYS[0], end_date=DAYS[-1],
                                  source_approval_ref="reviewed-rights"),
        dividend_coverage_row(start_date=DAYS[0], end_date=DAYS[-1],
                              source_approval_ref="reviewed-dividend"),
    ], columns=ACTION_EVENT_COLUMNS)
    facts = pd.DataFrame([{"date": day, "instrument": NAME} for day in DAYS])
    membership = pd.DataFrame([{"date": day, "instrument": NAME} for day in DAYS])
    membership_set = {
        "binding_type": "index_membership_set", "universe_id": "a500",
        "index_code": "000510.SH", "window_start": DAYS[0], "window_end": DAYS[-1],
        "snapshots": [{"snapshot_date": "2025-01-01"}],
    }
    membership_set["set_digest_sha256"] = sha256_json(membership_set)
    monkeypatch.setattr(runner, "_verified_result", lambda *_: (strategy, config, {}))
    monkeypatch.setattr(runner, "read_pit_benchmark_membership",
                        lambda *_args, **_kwargs: (membership.copy(), membership_set))

    class MarketStore:
        def __init__(self, _root):
            pass

        def read_frame(self, _generation, _checksum, *, dataset_type, required_columns):
            if dataset_type == "calendar":
                return pd.DataFrame({"date": DAYS})
            if dataset_type == "suspension":
                return pd.DataFrame(columns=["date", "instrument", "suspended"])
            assert dataset_type == "corporate_action_events"
            assert set(required_columns) <= set(events.columns)
            return events

    class UniverseStore:
        def __init__(self, _root):
            pass

        def read_manifest(self, _generation):
            return {"universe_id": "a500", "members_artifact": {"checksum_sha256": "9" * 64}}

        def read_members(self, *_args, **_kwargs):
            return pd.DataFrame({"instrument": [NAME]})

    class RawPrices:
        def __init__(self, _root):
            pass

        def read_window(self, *_args):
            return config["price_source_binding"], pd.DataFrame([
                {"session_date": day, "instrument": NAME, "open": 10., "close": 10., "volume": 10000.}
                for day in DAYS
            ])

    class FactStore:
        def __init__(self, _root):
            pass

        def read(self, _generation):
            return {"synthetic": True}, facts

    class Engine:
        def __init__(self, _root):
            pass

        def run(self, **kwargs):
            assert kwargs["config"]["contract_version"] == 4
            pd.testing.assert_frame_equal(kwargs["corporate_action_events"], events)
            assert kwargs["corporate_action_instruments"] == set()
            assert kwargs["weight_partitions"].keys() == set(DAYS)
            return {"generation_id": "a" * 64, "manifest_digest_sha256": "b" * 64}, {
                "equity_curve": pd.DataFrame({"holdings_value": [0.]})
            }

    class ResultStore:
        def __init__(self, _root):
            self.manifest = None

        def publish(self, manifest, _frames, *, quality_decision, target_weight_generations):
            assert quality_decision == {"external": "review"}
            assert len(target_weight_generations) == len(DAYS)
            self.manifest = manifest

        def read(self, _generation):
            return self.manifest, {}

    monkeypatch.setattr(runner, "BacktestMarketDatasetStore", MarketStore)
    monkeypatch.setattr(runner, "UniverseSnapshotStore", UniverseStore)
    monkeypatch.setattr(runner, "RawExecutionPriceStore", RawPrices)
    monkeypatch.setattr(runner, "ExecutionFactsStore", FactStore)
    monkeypatch.setattr(runner, "facts_binding", lambda _manifest: config["execution_facts_binding"])
    monkeypatch.setattr(runner, "BacktestEngine", Engine)
    monkeypatch.setattr(runner, "BacktestResultStore", ResultStore)

    report = runner.publish_benchmark_backtest(
        data_root=tmp_path / "data", benchmark_root=tmp_path / "benchmark",
        membership_root=tmp_path / "membership", strategy_result_generation_id=strategy["generation_id"],
        scheme="equal_weight", quality_decision={"external": "review"},
    )
    assert report["scheme"] == "equal_weight"
    assert report["backtest_result_generation_id"] == "a" * 64
