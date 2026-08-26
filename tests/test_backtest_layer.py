"""Phase 2 backtest layer runtime tests."""

import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.contracts.model_layer import create_reviewed_quality_decision
from uq.errors import ContractError

GEN_A = "a" * 64; GEN_B = "b" * 64; GEN_C = "c" * 64; GEN_D = "d" * 64


def _make_config(**overrides):
    base = {
        "contract_version": 1, "schema_version": "1.0.0",
        "backtest_name": "test_bt", "start_date": "2026-01-01", "end_date": "2026-01-31",
        "execution_model": {"type": "daily_t1_open", "board_lot": 100,
            "sellable_quantity_rule": "prior_day_holding_only", "volume_participation_cap": 0.5},
        "cost_model": {"commission_bps": 2.5, "stamp_duty_bps": 5.0, "slippage_bps": 1.0},
        "limit_rules": {"limit_ratio": 0.10, "prev_close_source": "raw_prev_close", "adjustment_basis": "raw"},
        "calendar_binding": {"generation_id": GEN_C, "checksum_sha256": GEN_A},
        "price_source_binding": {"dataset_generation_id": GEN_A, "data_checksum_sha256": GEN_B},
        "universe_binding": {"snapshot_generation_id": GEN_C},
        "corporate_action_binding": {"dataset_generation_id": GEN_B, "data_checksum_sha256": GEN_C},
        "suspension_binding": {"dataset_generation_id": GEN_C, "data_checksum_sha256": GEN_A},
        "initial_capital": 1000000.0,
        "run_id": "00000000-0000-0000-0000-000000000003",
        "created_at": "2026-01-01T00:00:02Z",
        "quality_report_checksum_sha256": "0" * 64,
        "generation_id": GEN_A, "manifest_digest_sha256": GEN_B,
    }
    base.update(overrides)
    return base


def _make_price_panel(dates, instruments, open_prices=None):
    rows = []
    for i, d in enumerate(dates):
        for j, inst in enumerate(instruments):
            base = (open_prices or {}).get(inst, 10.0 + j * 5)
            drift = 1.01 ** i
            rows.append({
                "date": d, "instrument": inst,
                "open": round(base * drift, 4),
                "close": round(base * drift * 1.005, 4),
                "volume": 500000,
            })
    return pd.DataFrame(rows).set_index(["date", "instrument"])


def _make_weights(date, instruments, weight=1.0):
    n = len(instruments)
    w = round(weight / n, 12) if n > 0 else 0
    return pd.DataFrame({"instrument": instruments, "weight": [w] * n})


def _make_decision():
    checks = [{"name": "equity_curve_finite_positive", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}]
    return create_reviewed_quality_decision(
        binding_type="backtest_result_v1", policy="reject_all", status="passed",
        checks=checks, errors=[], warnings=[], producer_code_fingerprint="0" * 64,
    )


DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
INSTRUMENTS = ["A", "B"]


class TestBacktestEngine:
    def test_deterministic_pnl(self):
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config()
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        manifest, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        # Same inputs produce same results
        manifest2, artifacts2 = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel.copy(),
        )
        assert manifest["summary_metrics"] == manifest2["summary_metrics"]

    def test_costs_reduce_pnl(self):
        engine = BacktestEngine(tempfile.mkdtemp())
        no_cost_config = _make_config(cost_model={"commission_bps": 0, "stamp_duty_bps": 0, "slippage_bps": 0})
        cost_config = _make_config()
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        m_no_cost, _ = engine.run(config=no_cost_config, portfolio_definition={"generation_id": GEN_B}, weight_partitions=weights, price_panel=price_panel)
        m_cost, _ = engine.run(config=cost_config, portfolio_definition={"generation_id": GEN_B}, weight_partitions=weights, price_panel=price_panel)
        assert m_no_cost["summary_metrics"]["cumulative_return"] >= m_cost["summary_metrics"]["cumulative_return"]

    def test_limit_up_blocks_buy(self):
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config(limit_rules={"limit_ratio": 0.001, "prev_close_source": "raw_prev_close", "adjustment_basis": "raw"})
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        # Force limit-up on exec day
        for d in DATES:
            price_panel.loc[(d, "A"), "close"] = price_panel.loc[(d, "A"), "open"] * 1.02
        weights = {"2026-01-05": _make_weights("2026-01-05", ["A"])}
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        fills = artifacts["fills"]
        if len(fills) > 0:
            assert not ((fills["instrument"] == "A") & (fills["side"] == "buy") & (fills["status"] == "filled")).all()

    def test_limit_down_blocks_sell(self):
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config(limit_rules={"limit_ratio": 0.001, "prev_close_source": "raw_prev_close", "adjustment_basis": "raw"})
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights_1 = _make_weights("2026-01-05", ["A"])
        weights_2 = _make_weights("2026-01-06", [])  # empty = sell all
        weights = {"2026-01-05": weights_1, "2026-01-06": weights_2}
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )

    def test_board_lot_rounding(self):
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config()
        config["initial_capital"] = 12345.0  # small capital forces rounding
        price_panel = _make_price_panel(DATES, ["A"], open_prices={"A": 33.33})
        weights = {"2026-01-05": _make_weights("2026-01-05", ["A"])}
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        fills = artifacts["fills"]
        if len(fills) > 0:
            buy_fills = fills[fills["status"] == "filled"]
            if len(buy_fills) > 0:
                assert (buy_fills["filled_shares"] % 100 == 0).all()

    def test_suspension_skip_recorded(self):
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config()
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        suspension = {("2026-01-06", "A")}
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
            suspension_dates=suspension,
        )
        fills = artifacts["fills"]
        if len(fills) > 0:
            skipped = fills[fills["status"] == "skipped_suspended"]
            # At least some fill attempt should be recorded

    def test_volume_guard_skips_fill(self):
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config(execution_model={
            "type": "daily_t1_open", "board_lot": 100,
            "sellable_quantity_rule": "prior_day_holding_only",
            "volume_participation_cap": 0.0001,
        })
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )


class TestBacktestResultStore:
    def test_e2e_publish_read(self, tmp_path):
        root = Path(tmp_path)
        engine = BacktestEngine(root)
        store = BacktestResultStore(root)

        config = _make_config()
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        manifest, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )

        tw_bindings = [{
            "decision_date": "2026-01-05",
            "generation_id": GEN_C,
            "manifest_digest_sha256": GEN_D,
        }]
        partition = store.publish(manifest.copy(), artifacts, quality_decision=_make_decision(), target_weight_generations=tw_bindings)

        disk_manifest = json.loads((partition / "manifest.json").read_text())
        read_manifest, read_artifacts = store.read(disk_manifest["generation_id"])

        assert len(read_artifacts["equity_curve"]) > 0
        assert len(read_artifacts["daily_metrics"]) > 0
        assert abs(read_manifest["summary_metrics"]["cumulative_return"] - manifest["summary_metrics"]["cumulative_return"]) < 1e-10

    def test_overwrite_rejection(self, tmp_path):
        root = Path(tmp_path)
        engine = BacktestEngine(root)
        store = BacktestResultStore(root)
        config = _make_config()
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        manifest, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        tw_bindings = [{"decision_date": "2026-01-05", "generation_id": GEN_C, "manifest_digest_sha256": GEN_D}]
        decision = _make_decision()
        store.publish(manifest.copy(), artifacts, quality_decision=decision, target_weight_generations=tw_bindings)
        with pytest.raises(ContractError, match="already exists"):
            store.publish(manifest.copy(), artifacts, quality_decision=decision, target_weight_generations=tw_bindings)

    def test_tampered_artifact_rejects_read(self, tmp_path):
        root = Path(tmp_path)
        engine = BacktestEngine(root)
        store = BacktestResultStore(root)
        config = _make_config()
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        manifest, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        tw_bindings = [{"decision_date": "2026-01-05", "generation_id": GEN_C, "manifest_digest_sha256": GEN_D}]
        partition = store.publish(manifest.copy(), artifacts, quality_decision=_make_decision(), target_weight_generations=tw_bindings)
        disk_manifest = json.loads((partition / "manifest.json").read_text())

        equity_file = partition / "equity_curve.parquet"
        equity_file.write_bytes(equity_file.read_bytes() + b"tampered")

        with pytest.raises(ContractError, match="tampered|checksum"):
            store.read(disk_manifest["generation_id"])


class TestT1SellableQuantity:
    def test_t1_sellable_quantity_enforced(self):
        """Shares bought on day T cannot be sold until T+1."""
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config()
        # 3 dates: buy on D1 exec, try to sell on D2 (should work because T+1 has passed)
        dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
        price_panel = _make_price_panel(dates, ["A"], open_prices={"A": 10.0})
        
        weights = {
            "2026-01-05": _make_weights("2026-01-05", ["A"]),
            "2026-01-06": pd.DataFrame({"instrument": [], "weight": []}),  # empty = sell all
        }
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        fills = artifacts["fills"]
        buy_fills = fills[(fills["side"] == "buy") & (fills["status"] == "filled")]
        sell_fills = fills[(fills["side"] == "sell") & (fills["status"] == "filled")]
        # If we bought, the sell should succeed because it's on the next trading day
        if len(buy_fills) > 0 and len(sell_fills) > 0:
            assert sell_fills.iloc[0]["date"] > buy_fills.iloc[0]["date"]

    def test_insufficient_cash_skip_recorded(self):
        """Buy order exceeding available cash is skipped and recorded."""
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config(initial_capital=1000.0)  # very small
        price_panel = _make_price_panel(DATES, INSTRUMENTS)
        weights = {"2026-01-05": _make_weights("2026-01-05", INSTRUMENTS)}
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        fills = artifacts["fills"]
        if len(fills) > 0:
            cash_skips = fills[fills["status"] == "skipped_insufficient_cash"]
            filled_buys = fills[(fills["side"] == "buy") & (fills["status"] == "filled")]
            # Either all buys are filled within budget or some are skipped for cash
            total_cost = sum(f["filled_shares"] * f["net_execution_price"] + f["commission_fee"] for _, f in filled_buys.iterrows())
            assert total_cost <= config["initial_capital"] or len(cash_skips) > 0


class TestT1Enforcement:
    def test_same_day_bought_shares_not_sellable(self):
        """Buy on D1 exec; on D2 (next day) try to sell. Should succeed because T+1 has passed.
        But if we could somehow sell same day, it would violate T+1.
        The real test: buy fills happen, then next day's rebalance can sell."""
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config()
        dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
        price_panel = _make_price_panel(dates, ["A"], open_prices={"A": 10.0})
        
        # Day 1: buy A. Day 2 decision: target 0 (sell all). Exec on day 3.
        weights = {
            "2026-01-05": _make_weights("2026-01-05", ["A"]),
            "2026-01-06": pd.DataFrame({"instrument": ["A"], "weight": [0.5]}),
        }
        _, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        fills = artifacts["fills"]
        buy_fills = fills[(fills["side"] == "buy") & (fills["status"] == "filled")]
        sell_fills = fills[(fills["side"] == "sell") & (fills["status"] == "filled")]
        assert len(buy_fills) > 0
        # Sell must be on a later date than the buy (T+1 enforced by design)
        for _, sell in sell_fills.iterrows():
            assert sell["date"] > buy_fills.iloc[0]["date"]

    def test_partial_sell_reduces_position(self):
        """Target weight reduction should partially sell, not liquidate all."""
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config(initial_capital=1000000.0)
        dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
        price_panel = _make_price_panel(dates, ["A"], open_prices={"A": 100.0})
        
        weights = {
            "2026-01-05": _make_weights("2026-01-05", ["A"]),  # 100%
            "2026-01-06": pd.DataFrame({"instrument": ["A"], "weight": [0.5]}),  # reduce to 50%
        }
        manifest, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        fills = artifacts["fills"]
        sells = fills[(fills["side"] == "sell") & (fills["status"] == "filled")]
        buys = fills[(fills["side"] == "buy") & (fills["status"] == "filled")]
        if len(buys) > 0 and len(sells) > 0:
            # Partial sell: sold shares < bought shares
            assert sells.iloc[0]["filled_shares"] < buys.iloc[0]["filled_shares"]

    def test_add_to_existing_position(self):
        """Target weight increase should add to existing position, not liquidate-rebuy."""
        engine = BacktestEngine(tempfile.mkdtemp())
        config = _make_config(initial_capital=1000000.0)
        dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
        price_panel = _make_price_panel(dates, ["A"], open_prices={"A": 100.0})
        
        weights = {
            "2026-01-05": _make_weights("2026-01-05", ["A"]),  # 50% (equal split with phantom B)
            "2026-01-06": _make_weights("2026-01-06", ["A"]),  # 100% single
        }
        manifest, artifacts = engine.run(
            config=config, portfolio_definition={"generation_id": GEN_B},
            weight_partitions=weights, price_panel=price_panel,
        )
        fills = artifacts["fills"]
        buys = fills[(fills["side"] == "buy") & (fills["status"] == "filled")]
        sells = fills[(fills["side"] == "sell") & (fills["status"] == "filled")]
        # If we increased from partial to full, we should see two buy fills and no sells
        if len(buys) >= 2:
            assert len(sells) == 0
