"""Backtest layer: simulate execution of target weights with costs and guards."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as arrow
import pyarrow.parquet as parquet

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    model_manifest_identities,
)
from ..errors import ContractError

ANNUALIZATION_DAYS = 252


class BacktestEngine:
    """Daily T+1-open backtest engine."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def run(
        self,
        *,
        config: dict[str, Any],
        portfolio_definition: dict[str, Any],
        weight_partitions: dict[str, pd.DataFrame],
        price_panel: pd.DataFrame,
        suspension_dates: set[tuple[str, str]] | None = None,
        corporate_action_instruments: set[str] | None = None,
        quality_decision: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
        """Run a deterministic daily backtest.

        Args:
            config: validated backtest_config.v1 manifest.
            portfolio_definition: for constraint metadata.
            weight_partitions: {decision_date: DataFrame[instrument, weight]}.
            price_panel: DataFrame with MultiIndex (date, instrument), columns
                [open, close, volume].
            suspension_dates: {(date, instrument)} pairs marked suspended.
            corporate_action_instruments: instruments with corp actions in period.
            quality_decision: external reviewed quality decision.

        Returns:
            (manifest, artifacts) where artifacts has keys equity_curve,
            daily_metrics, fills.
        """
        cost_model = config["cost_model"]
        commission_rate = cost_model["commission_bps"] / 10000.0
        stamp_duty_rate = cost_model["stamp_duty_bps"] / 10000.0
        slippage_rate = cost_model["slippage_bps"] / 10000.0
        board_lot = config["execution_model"].get("board_lot", 100)
        participation_cap = config["execution_model"].get("volume_participation_cap", 0.10)
        limit_ratio = config["limit_rules"]["limit_ratio"]

        initial_capital = config["initial_capital"]
        start_date = config["start_date"]
        end_date = config["end_date"]

        # Sort decision dates
        sorted_dates = sorted(weight_partitions.keys())
        if not sorted_dates:
            raise ContractError("no weight partitions provided")

        trading_dates = sorted(set(price_panel.index.get_level_values("date")))
        trading_dates = [d for d in trading_dates if start_date <= d <= end_date]
        if not trading_dates:
            raise ContractError(f"no trading dates between {start_date} and {end_date}")

        cash = initial_capital
        holdings: dict[str, int] = {}  # instrument -> total shares held
        t1_locked: dict[str, int] = {}  # instrument -> shares bought today (not yet sellable)
        prev_close_prices: dict[str, float] = {}
        fills_rows: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        metrics_rows: list[dict[str, Any]] = []


        for i, date in enumerate(trading_dates):
            day_data = price_panel.loc[date]

            # --- Mark to market at close ---
            total_holdings_value = 0.0
            for inst, shares in holdings.items():
                if inst in day_data.index:
                    close_price = day_data.loc[inst, "close"]
                    if pd.isna(close_price) or close_price <= 0:
                        raise ContractError(f"missing or invalid close price for {inst} on {date}")
                    total_holdings_value += shares * float(close_price)
                else:
                    raise ContractError(f"price data missing for held instrument {inst} on {date}")
            nav = cash + total_holdings_value

            equity_rows.append({
                "date": date,
                "portfolio_value": nav,
                "cash": cash,
                "holdings_value": total_holdings_value,
            })

            daily_return = ((nav / equity_rows[-2]["portfolio_value"]) - 1.0) if i > 0 and equity_rows[-2]["portfolio_value"] > 0 else 0.0

            day_fills = [f for f in fills_rows if f["date"] == date and f["status"] == "filled"]
            day_turnover_value = sum(abs(f["filled_shares"] * f["net_execution_price"]) for f in day_fills)
            executed_turnover = (day_turnover_value / nav / 2.0) if nav > 0 else 0.0

            metrics_rows.append({
                "date": date,
                "daily_return": daily_return,
                "executed_turnover": round(executed_turnover, 10),
                "cash_ratio": round(cash / nav, 8) if nav > 0 else 1.0,
            })

            # --- Rebalance: check if this decision date has targets ---
            if date not in weight_partitions:
                for inst in day_data.index:
                    cp = day_data.loc[inst, "close"]
                    if not pd.isna(cp) and cp > 0:
                        prev_close_prices[inst] = float(cp)
                continue

            weights_df = weight_partitions[date]
            target_weights = dict(zip(weights_df["instrument"], weights_df["weight"])) if len(weights_df) > 0 else {}
            
            exec_date_idx = i + 1
            if exec_date_idx >= len(trading_dates):
                break
            exec_date = trading_dates[exec_date_idx]
            exec_day_data = price_panel.loc[exec_date]

            # --- Compute target share counts using T-close NAV (spec formula) ---
            target_shares_map: dict[str, int] = {}
            for inst, target_w in sorted(target_weights.items()):
                open_price = exec_day_data.loc[inst, "open"] if inst in exec_day_data.index else None
                if open_price is None or pd.isna(open_price) or open_price <= 0:
                    continue
                raw_target = target_w * nav / (float(open_price) * (1 + slippage_rate))
                floored = int(raw_target / board_lot) * board_lot
                target_shares_map[inst] = max(floored, 0)

            # --- Execute sells first (only prior-day sellable shares) ---
            for inst in sorted(set(list(holdings.keys()) + list(target_shares_map.keys()))):
                current_total = holdings.get(inst, 0)
                current_sellable = current_total - t1_locked.get(inst, 0)
                target_shares = target_shares_map.get(inst, 0)
                
                open_price = exec_day_data.loc[inst, "open"] if inst in exec_day_data.index else None
                volume = exec_day_data.loc[inst, "volume"] if inst in exec_day_data.index else 0.0
                pit_volume = day_data.loc[inst, "volume"] if inst in day_data.index else 0.0
                
                is_suspended = (
                    open_price is None or pd.isna(open_price) or open_price <= 0
                    or volume <= 0
                    or (exec_date, inst) in (suspension_dates or set())
                )
                
                if is_suspended:
                    if current_sellable > 0:
                        fills_rows.append(self._fill_row(
                            date=exec_date, instrument=inst, side="sell",
                            target_shares=current_sellable, filled_shares=0,
                            gross_execution_price=0.0, net_execution_price=0.0,
                            commission_fee=0, stamp_duty_fee=0, status="skipped_suspended",
                        ))
                    continue
                
                prev_close = prev_close_prices.get(inst, float(open_price))
                limit_down_price = prev_close * (1 - limit_ratio)
                limit_up_price = prev_close * (1 + limit_ratio)
                
                if target_shares < current_total:
                    # SELL: reduce to target or liquidate
                    shares_to_sell = min(current_sellable, current_total - target_shares)
                    if shares_to_sell <= 0:
                        if current_total - target_shares > 0:
                            fills_rows.append(self._fill_row(
                                date=exec_date, instrument=inst, side="sell",
                                target_shares=current_total - target_shares, filled_shares=0,
                                gross_execution_price=float(open_price), net_execution_price=float(open_price),
                                commission_fee=0, stamp_duty_fee=0, status="skipped_t1_not_sellable",
                            ))
                        continue
                    
                    if float(open_price) <= limit_down_price:
                        fills_rows.append(self._fill_row(
                            date=exec_date, instrument=inst, side="sell",
                            target_shares=shares_to_sell, filled_shares=0,
                            gross_execution_price=float(open_price), net_execution_price=float(open_price),
                            commission_fee=0, stamp_duty_fee=0, status="skipped_limit_down",
                        ))
                        continue
                    
                    volume_cap = int(pit_volume * participation_cap / board_lot) * board_lot
                    if shares_to_sell > volume_cap:
                        fills_rows.append(self._fill_row(
                            date=exec_date, instrument=inst, side="sell",
                            target_shares=shares_to_sell, filled_shares=0,
                            gross_execution_price=float(open_price), net_execution_price=float(open_price),
                            commission_fee=0, stamp_duty_fee=0, status="skipped_volume",
                        ))
                        continue
                    
                    net_price = float(open_price) * (1 - slippage_rate)
                    gross_value = shares_to_sell * float(open_price)
                    commission = gross_value * commission_rate
                    stamp_duty = gross_value * stamp_duty_rate
                    proceeds = gross_value - commission - stamp_duty
                    cash += proceeds
                    holdings[inst] = current_total - shares_to_sell
                    if holdings[inst] <= 0:
                        del holdings[inst]
                    fills_rows.append(self._fill_row(
                        date=exec_date, instrument=inst, side="sell",
                        target_shares=shares_to_sell, filled_shares=shares_to_sell,
                        gross_execution_price=float(open_price), net_execution_price=net_price,
                        commission_fee=commission, stamp_duty_fee=stamp_duty, status="filled",
                    ))
                    
                elif target_shares > current_total:
                    # BUY: increase position or open new
                    shares_to_buy = target_shares - current_total
                    
                    if float(open_price) >= limit_up_price:
                        fills_rows.append(self._fill_row(
                            date=exec_date, instrument=inst, side="buy",
                            target_shares=shares_to_buy, filled_shares=0,
                            gross_execution_price=float(open_price), net_execution_price=float(open_price),
                            commission_fee=0, stamp_duty_fee=0, status="skipped_limit_up",
                        ))
                        continue
                    
                    volume_cap = int(pit_volume * participation_cap / board_lot) * board_lot
                    actual_shares = min(shares_to_buy, max(volume_cap, 0))
                    if actual_shares <= 0:
                        fills_rows.append(self._fill_row(
                            date=exec_date, instrument=inst, side="buy",
                            target_shares=shares_to_buy, filled_shares=0,
                            gross_execution_price=float(open_price), net_execution_price=float(open_price),
                            commission_fee=0, stamp_duty_fee=0, status="skipped_volume",
                        ))
                        continue
                    
                    net_price = float(open_price) * (1 + slippage_rate)
                    cost = actual_shares * net_price
                    commission = cost * commission_rate
                    total_cost = cost + commission
                    if total_cost > cash:
                        fills_rows.append(self._fill_row(
                            date=exec_date, instrument=inst, side="buy",
                            target_shares=actual_shares, filled_shares=0,
                            gross_execution_price=float(open_price), net_execution_price=net_price,
                            commission_fee=0, stamp_duty_fee=0, status="skipped_insufficient_cash",
                        ))
                        continue
                    
                    cash -= total_cost
                    holdings[inst] = current_total + actual_shares
                    t1_locked[inst] = t1_locked.get(inst, 0) + actual_shares
                    fills_rows.append(self._fill_row(
                        date=exec_date, instrument=inst, side="buy",
                        target_shares=actual_shares, filled_shares=actual_shares,
                        gross_execution_price=float(open_price), net_execution_price=net_price,
                        commission_fee=commission, stamp_duty_fee=0, status="filled",
                    ))
            
            # T+1 unlock: all previously locked shares become sellable tomorrow
            t1_locked.clear()

            # Update prev_close from execution day
            for inst in exec_day_data.index:
                cp = exec_day_data.loc[inst, "close"]
                if not pd.isna(cp) and cp > 0:
                    prev_close_prices[inst] = float(cp)

# Compute summary metrics
        equity_df = pd.DataFrame(equity_rows)
        metrics_df = pd.DataFrame(metrics_rows)
        fills_df = pd.DataFrame(fills_rows) if fills_rows else pd.DataFrame(columns=[
            "date", "instrument", "side", "target_shares", "filled_shares",
            "gross_execution_price", "net_execution_price", "commission_fee",
            "stamp_duty_fee", "status"
        ])

        returns = metrics_df["daily_return"].values
        n_days = len(returns)
        final_value = equity_df["portfolio_value"].iloc[-1] if len(equity_df) > 0 else initial_capital

        cumulative_return = final_value / initial_capital - 1.0
        annualized_return = (
            (final_value / initial_capital) ** (ANNUALIZATION_DAYS / max(n_days, 1)) - 1.0
        ) if n_days > 0 else 0.0
        annualized_volatility = float(np.std(returns, ddof=1)) * np.sqrt(ANNUALIZATION_DAYS) if n_days > 1 else 0.0

        if annualized_volatility > 1e-12 and n_days > 1:
            sharpe_ratio = float(np.mean(returns) / np.std(returns, ddof=1)) * np.sqrt(ANNUALIZATION_DAYS)
        else:
            sharpe_ratio = None

        running_max = equity_df["portfolio_value"].cummax()
        drawdowns = 1.0 - equity_df["portfolio_value"] / running_max
        max_drawdown = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0

        avg_daily_turnover = float(metrics_df["executed_turnover"].mean()) if n_days > 0 else None
        positive_days = (metrics_df["daily_return"] > 0).sum()
        win_rate = float(positive_days / n_days) if n_days > 0 else None

        summary_metrics = {
            "cumulative_return": round(cumulative_return, 10),
            "annualized_return": round(annualized_return, 10),
            "annualized_volatility": round(max(annualized_volatility, 0.0), 10),
            "sharpe_ratio": round(sharpe_ratio, 6) if sharpe_ratio is not None else None,
            "max_drawdown": round(max_drawdown, 8),
            "avg_daily_turnover": round(avg_daily_turnover, 8) if avg_daily_turnover is not None else None,
            "win_rate": round(win_rate, 6) if win_rate is not None else None,
        }

        manifest = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "backtest_config_generation_id": config["generation_id"],
            "portfolio_definition_generation_id": portfolio_definition["generation_id"],
            "target_weight_bindings": [],
            "price_source_binding": config["price_source_binding"],
            "period_start": start_date,
            "period_end": end_date,
            "trading_days": max(n_days, 1),
            "equity_curve_artifact": {
                "file": "equity_curve.parquet", "checksum_sha256": "0" * 64,
                "row_count": len(equity_df), "columns": list(equity_df.columns),
                "dtypes": {c: str(equity_df[c].dtype) for c in equity_df.columns},
                "serialization_profile_id": "parquet-v1",
            },
            "daily_metrics_artifact": {
                "file": "daily_metrics.parquet", "checksum_sha256": "0" * 64,
                "row_count": len(metrics_df), "columns": list(metrics_df.columns),
                "dtypes": {c: str(metrics_df[c].dtype) for c in metrics_df.columns},
                "serialization_profile_id": "parquet-v1",
            },
            "fills_artifact": {
                "file": "fills.parquet", "checksum_sha256": "0" * 64,
                "row_count": len(fills_df), "columns": list(fills_df.columns),
                "dtypes": {c: str(fills_df[c].dtype) for c in fills_df.columns},
                "serialization_profile_id": "parquet-v1",
            },
            "summary_metrics": summary_metrics,
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "quality_report_checksum_sha256": "0" * 64,
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }

        artifacts = {"equity_curve": equity_df, "daily_metrics": metrics_df, "fills": fills_df}
        return manifest, artifacts

    @staticmethod
    def _fill_row(*, date: str, instrument: str, side: str,
                  target_shares: int, filled_shares: int,
                  gross_execution_price: float, net_execution_price: float,
                  commission_fee: float, stamp_duty_fee: float,
                  status: str) -> dict[str, Any]:
        return {
            "date": date, "instrument": instrument, "side": side,
            "target_shares": target_shares, "filled_shares": filled_shares,
            "gross_execution_price": gross_execution_price,
            "net_execution_price": net_execution_price,
            "commission_fee": commission_fee, "stamp_duty_fee": stamp_duty_fee,
            "status": status,
        }


class BacktestResultStore:
    """Publish and read immutable backtest result partitions."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.results_dir = self.root / "backtest_results"
        self.reviews_dir = self.root / "external_quality_reviews"

    def publish(
        self,
        manifest: dict[str, Any],
        artifacts: dict[str, pd.DataFrame],
        *,
        quality_decision: dict[str, Any],
        target_weight_generations: list[dict[str, str]],
    ) -> Path:
        """Publish backtest result partition atomically."""
        manifest["target_weight_bindings"] = target_weight_generations

        artifact_files = {
            "equity_curve": manifest["equity_curve_artifact"],
            "daily_metrics": manifest["daily_metrics_artifact"],
            "fills": manifest["fills_artifact"],
        }
        artifact_frames = {
            "equity_curve": artifacts["equity_curve"],
            "daily_metrics": artifacts["daily_metrics"],
            "fills": artifacts["fills"],
        }
        serialized: dict[str, bytes] = {}
        for key, meta in artifact_files.items():
            data, checksum = self._serialize(artifact_frames[key])
            serialized[key] = data
            meta["checksum_sha256"] = checksum

        provisional_gen, _ = model_manifest_identities(
            {**manifest, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="backtest_result",
        )

        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="backtest_result_v1",
            subject_generation_id=provisional_gen,
        )
        manifest["quality_report_checksum_sha256"] = report_checksum

        final_gen, digest = model_manifest_identities(
            {**manifest, "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="backtest_result",
        )
        manifest["generation_id"] = final_gen
        manifest["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("backtest_result", manifest)

        partition = self.results_dir / f"result={final_gen}"
        if partition.exists():
            raise ContractError(f"backtest result already exists: {partition}")

        staging = partition.parent / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            for key, meta in artifact_files.items():
                (staging / meta["file"]).write_bytes(serialized[key])
            (staging / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + "\n"
            )
            self.reviews_dir.mkdir(parents=True, exist_ok=True)
            review_path = self.reviews_dir / f"{report_checksum}.json"
            if not review_path.exists():
                review_path.write_text(json.dumps(bound_report, sort_keys=True, indent=2) + "\n")
            fsync_tree(staging)
            os.replace(staging, partition)
            fsync_dir(partition.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return partition

    def read(self, result_generation_id: str) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
        """Read a backtest result partition with fail-closed verification."""
        partition = self.results_dir / f"result={result_generation_id}"
        manifest_path = partition / "manifest.json"
        if not manifest_path.is_file():
            raise ContractError(f"unpublished backtest result: {partition}")

        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed backtest result manifest") from exc

        ModelContractLoader.validate("backtest_result", manifest)
        if manifest["generation_id"] != result_generation_id:
            raise ContractError("backtest result generation mismatch on read")

        checksum = manifest["quality_report_checksum_sha256"]
        review_path = self.reviews_dir / f"{checksum}.json"
        if not review_path.is_file():
            raise ContractError("backtest quality report unavailable")
        report = json.loads(review_path.read_text())
        ModelContractLoader.validate("model_quality_report", report)
        if report.get("binding_type") != "backtest_result_v1":
            raise ContractError("quality report binding type mismatch")
        if report.get("status") not in ("passed", "warning"):
            raise ContractError("quality report rejects read")

        prov_gen, _ = model_manifest_identities(
            {**manifest, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="backtest_result",
        )
        if report.get("bound_generation_id") != prov_gen:
            raise ContractError("quality report does not bind this generation")

        artifacts: dict[str, pd.DataFrame] = {}
        artifact_metas = [
            ("equity_curve", manifest["equity_curve_artifact"]),
            ("daily_metrics", manifest["daily_metrics_artifact"]),
            ("fills", manifest["fills_artifact"]),
        ]
        for key, meta in artifact_metas:
            file_path = partition / meta["file"]
            if not file_path.is_file():
                raise ContractError(f"missing artifact file: {meta['file']}")
            actual_checksum = file_sha256_bytes(file_path.read_bytes())
            if actual_checksum != meta["checksum_sha256"]:
                raise ContractError(f"tampered artifact prevents read: {meta['file']}")
            frame = pd.read_parquet(file_path)
            if len(frame) != meta["row_count"]:
                raise ContractError(f"artifact row count mismatch: {meta['file']}")
            if list(frame.columns) != meta["columns"]:
                raise ContractError(f"artifact column mismatch: {meta['file']}")
            if meta.get("serialization_profile_id") != "parquet-v1":
                raise ContractError(f"unsupported serialization profile: {meta.get('serialization_profile_id')}")
            for col_name, expected_dtype in meta.get("dtypes", {}).items():
                if col_name in frame.columns:
                    actual_dtype = str(frame[col_name].dtype)
                    if "float" in expected_dtype and "float" not in actual_dtype:
                        raise ContractError(f"dtype mismatch for {col_name} in {meta['file']}: expected {expected_dtype}, got {actual_dtype}")
                    elif "int" in expected_dtype and "int" not in actual_dtype:
                        raise ContractError(f"dtype mismatch for {col_name} in {meta['file']}: expected {expected_dtype}, got {actual_dtype}")
                    elif expected_dtype == "string" and "object" not in actual_dtype and "str" not in actual_dtype:
                        raise ContractError(f"dtype mismatch for {col_name} in {meta['file']}: expected string, got {actual_dtype}")
            artifacts[key] = frame

        return manifest, artifacts

    @staticmethod
    def _serialize(frame: pd.DataFrame) -> tuple[bytes, str]:
        table = arrow.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)
        sink = arrow.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)
