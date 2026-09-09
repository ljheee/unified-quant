from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping
from typing import Any

import pandas as pd

from ..contracts.model_layer import ModelContractLoader, paper_execution_identities, sha256_json
from ..errors import ContractError


class PaperExecutionEngine:
    """Execute an immutable order plan against governed paper market data."""

    COLUMNS = [
        "instrument", "side", "order_state", "reject_reason", "filled_quantity",
        "requested_quantity", "price", "gross_amount", "fee_amount", "net_amount",
        "plan_sequence", "decision_date", "execution_date",
    ]

    DTYPES = {
        "instrument": "string", "side": "string", "order_state": "string",
        "reject_reason": "string", "filled_quantity": "int64",
        "requested_quantity": "int64", "price": "float64",
        "gross_amount": "float64", "fee_amount": "float64",
        "net_amount": "float64", "plan_sequence": "int64",
        "decision_date": "datetime64[ns]", "execution_date": "datetime64[ns]",
    }

    REJECT_REASONS = {
        "risk_blocked", "suspended", "limit_up", "limit_down",
        "insufficient_cash", "t1_not_sellable", "no_market_data", "lot_size",
        "quantity", "price", "cancelled", "none",
    }

    REQUIRED_MARKET_COLUMNS = {
        "open", "high", "low", "close", "volume", "status", "limit_up", "limit_down",
    }

    def execute(
        self,
        config: Mapping[str, Any],
        plan_manifest: Mapping[str, Any],
        plan_frame: pd.DataFrame,
        target_weights: pd.DataFrame,
        input_holdings: list[Mapping[str, Any]],
        execution_market: Mapping[str, Mapping[str, Any]],
        decision_prices: Mapping[str, Mapping[str, float]],
        *,
        opening_cash: float,
        suspended_instruments: Collection[str] = (),
        corporate_action_excluded: Collection[str] = (),
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        self._validate_config(config)
        self._validate_target_weights(target_weights)
        self._validate_plan(config, plan_manifest, plan_frame, input_holdings)
        if not math.isfinite(float(opening_cash)) or opening_cash < 0:
            raise ContractError("opening cash must be finite and non-negative")
        suspended = set(suspended_instruments)
        corporate_excluded = set(corporate_action_excluded)
        overlap = suspended & corporate_excluded
        if overlap:
            raise ContractError(f"instruments have overlapping risk exclusions: {sorted(overlap)}")

        holdings = self._holdings_by_instrument(input_holdings)
        decision_nav = self._decision_nav(opening_cash, holdings, decision_prices)
        events = self._execute_orders(
            config, plan_frame, execution_market, suspended, corporate_excluded, opening_cash
        )
        closing_holdings = self._closing_holdings(holdings, events)
        closing_cash = opening_cash + sum(float(row["net_amount"]) for row in events)
        if closing_cash < -1e-9:
            raise ContractError("execution creates negative closing cash")
        opening_value = self._portfolio_value(
            opening_cash, holdings, decision_prices, "decision", require_positive_price=True
        )
        closing_value = self._portfolio_value(
            closing_cash, closing_holdings, execution_market, "execution", require_positive_price=True
        )
        frame = pd.DataFrame(events, columns=self.COLUMNS).astype(self.DTYPES)
        aggregate = self._aggregate_reconciliation(
            config, target_weights, plan_frame, frame, decision_nav
        )
        manifest = self._manifest(
            config,
            plan_manifest,
            frame,
            aggregate,
            opening_cash=opening_cash,
            closing_cash=closing_cash,
            opening_portfolio_value=opening_value,
            closing_portfolio_value=closing_value,
        )
        return frame, manifest

    def _validate_config(self, config: Mapping[str, Any]) -> None:
        ModelContractLoader.validate("execution_config", dict(config))
        if config["mode"] != "paper" or config["market"] != "cn_a":
            raise ContractError("paper execution engine only supports cn_a paper mode")
        if config["decision_date"] > config["execution_date"]:
            raise ContractError("decision date must not follow execution date")
        if config["quantity_policy"]["sell_before_buy"] is not True:
            raise ContractError("paper execution requires sell-before-buy")
        if config["quantity_policy"]["partial_fills"] is not False:
            raise ContractError("paper execution does not support partial fills")

    def _validate_target_weights(self, target_weights: pd.DataFrame) -> None:
        if set(target_weights.columns) < {"instrument", "weight"}:
            raise ContractError("target weights must contain instrument and weight")
        if target_weights["instrument"].duplicated().any():
            raise ContractError("duplicate target instruments")
        weights = target_weights["weight"].map(float)
        if not weights.map(math.isfinite).all() or (weights < 0).any() or weights.sum() > 1 + 1e-9:
            raise ContractError("target weights are invalid")

    def _validate_plan(
        self,
        config: Mapping[str, Any],
        plan_manifest: Mapping[str, Any],
        plan_frame: pd.DataFrame,
        input_holdings: list[Mapping[str, Any]],
    ) -> None:
        required = {
            "execution_id", "state_mode", "execution_config_generation_id", "execution_date",
            "decision_date", "target_weights_binding", "input_state_binding",
            "initial_state_provenance_sha256", "market_data_binding", "calendar_binding",
            "suspension_binding", "corporate_action_binding", "risk_decision_binding",
            "generation_id", "manifest_digest_sha256",
        }
        if not required.issubset(plan_manifest):
            raise ContractError("order plan manifest has incomplete lineage")
        ModelContractLoader.validate("order_plan", dict(plan_manifest))
        expected_plan_generation, expected_plan_digest = paper_execution_identities(
            plan_manifest, schema_name="order_plan"
        )
        if (
            plan_manifest["generation_id"] != expected_plan_generation
            or plan_manifest["manifest_digest_sha256"] != expected_plan_digest
        ):
            raise ContractError("order plan manifest identity mismatch")
        if int(plan_manifest["row_count"]) != len(plan_frame):
            raise ContractError("order plan row count mismatch")
        if plan_manifest["key_uniqueness"] != ["plan_sequence"]:
            raise ContractError("order plan key uniqueness mismatch")
        if plan_manifest["execution_id"] != config["execution_id"]:
            raise ContractError("order plan execution id mismatch")
        if plan_manifest["state_mode"] != config["state_mode"]:
            raise ContractError("order plan state mode mismatch")
        if plan_manifest["execution_date"] != config["execution_date"]:
            raise ContractError("order plan execution date mismatch")
        if plan_manifest["decision_date"] != config["decision_date"]:
            raise ContractError("order plan decision date mismatch")
        if plan_manifest["execution_config_generation_id"] != config["generation_id"]:
            raise ContractError("order plan is bound to another execution config")
        expected_market_binding = {
            "family": config["market_data_binding"]["dataset_family"],
            "generation_id": config["market_data_binding"]["generation_id"],
            "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
        }
        if plan_manifest["market_data_binding"] != expected_market_binding:
            raise ContractError("order plan market_data_binding mismatch")
        for field in (
            "target_weights_binding", "input_state_binding", "calendar_binding",
            "suspension_binding", "corporate_action_binding", "risk_decision_binding",
        ):
            if plan_manifest[field] != config[field]:
                raise ContractError(f"order plan {field} mismatch")
        expected_initial_provenance = (
            config["initial_state"]["provenance_sha256"]
            if config["state_mode"] == "initial"
            else None
        )
        if plan_manifest.get("initial_state_provenance_sha256") != expected_initial_provenance:
            raise ContractError("order plan initial state provenance mismatch")

        if set(plan_frame.columns) != set(plan_manifest["columns"]) or len(plan_frame.columns) != len(plan_manifest["columns"]):
            raise ContractError("order plan columns do not match execution contract")
        frame = plan_frame.astype({
            "instrument": "string", "side": "string", "requested_quantity": "int64",
            "limit_price": "float64", "order_type": "string", "reason": "string",
            "target_delta_shares": "int64", "sellable_quantity": "int64",
            "previous_quantity": "int64", "target_quantity": "int64",
            "order_state": "string", "plan_sequence": "int64",
        })
        if frame["plan_sequence"].duplicated().any():
            raise ContractError("order plan sequence is not unique")
        if frame["plan_sequence"].tolist() != list(range(1, len(frame) + 1)):
            raise ContractError("order plan sequence is not contiguous")
        if not frame["order_state"].eq("planned").all():
            raise ContractError("order plan contains a non-planned order")
        if not frame["side"].isin(["buy", "sell"]).all():
            raise ContractError("order plan contains an invalid side")
        if not frame["order_type"].eq("limit").all():
            raise ContractError("order plan contains a non-limit order")
        if not frame["reason"].isin(["target_rebalance", "cash_residual", "risk_release"]).all():
            raise ContractError("order plan contains an invalid reason")
        if (frame["requested_quantity"] < 0).any() or (frame["sellable_quantity"] < 0).any():
            raise ContractError("order plan contains a negative quantity")
        if not frame["limit_price"].map(self._positive_float).all():
            raise ContractError("order plan contains an invalid limit price")
        if ((frame["side"] == "buy") & (frame["target_delta_shares"] < 0)).any():
            raise ContractError("buy plan contains a negative target delta")
        if ((frame["side"] == "sell") & (frame["target_delta_shares"] > 0)).any():
            raise ContractError("sell plan contains a positive target delta")
        sellable = self._sellable_quantities(config, input_holdings)
        for row in frame.to_dict("records"):
            instrument = str(row["instrument"])
            if row["side"] == "sell" and int(row["requested_quantity"]) > sellable.get(instrument, 0):
                raise ContractError(f"order plan sell exceeds sellable quantity for {instrument}")

    def _execute_orders(
        self,
        config: Mapping[str, Any],
        plan_frame: pd.DataFrame,
        execution_market: Mapping[str, Mapping[str, Any]],
        suspended: set[str],
        corporate_excluded: set[str],
        opening_cash: float,
    ) -> list[dict[str, Any]]:
        normalized = plan_frame.astype({
            "instrument": "string", "side": "string", "requested_quantity": "int64",
            "limit_price": "float64", "order_type": "string", "reason": "string",
            "target_delta_shares": "int64", "sellable_quantity": "int64",
            "previous_quantity": "int64", "target_quantity": "int64",
            "order_state": "string", "plan_sequence": "int64",
        })
        rows = normalized.to_dict("records")
        available_cash = float(opening_cash)
        fills: dict[int, dict[str, Any]] = {}
        for row in (item for item in rows if item["side"] == "sell"):
            fills[int(row["plan_sequence"])] = self._evaluate_event(
                config, row, execution_market, suspended, corporate_excluded
            )
            event = fills[int(row["plan_sequence"])]
            if event["order_state"] == "filled":
                available_cash += float(event["net_amount"])
        for row in (item for item in rows if item["side"] == "buy"):
            event = self._evaluate_event(
                config, row, execution_market, suspended, corporate_excluded,
                available_cash=available_cash,
            )
            fills[int(row["plan_sequence"])] = event
            if event["order_state"] == "filled":
                available_cash += float(event["net_amount"])
        return [fills[int(row["plan_sequence"])] for row in rows]

    def _evaluate_event(
        self,
        config: Mapping[str, Any],
        row: Mapping[str, Any],
        execution_market: Mapping[str, Mapping[str, Any]],
        suspended: set[str],
        corporate_excluded: set[str],
        *,
        available_cash: float | None = None,
    ) -> dict[str, Any]:
        instrument = str(row["instrument"])
        requested = int(row["requested_quantity"])
        reason = "none"
        if requested <= 0:
            reason = "quantity"
        market = execution_market.get(instrument)
        if instrument in corporate_excluded or instrument in suspended or (market or {}).get("status") != "trading":
            reason = "suspended"
        elif market is None or not self.REQUIRED_MARKET_COLUMNS.issubset(market):
            reason = "no_market_data"
        else:
            if config["price_policy"]["order_pricing_basis"] == "execution_open_replay":
                price, valid_price = self._numeric(market.get("open"))
            else:
                price, valid_price = self._numeric(row.get("limit_price"))
            limit_up, valid_limit_up = self._numeric(market.get("limit_up"))
            limit_down, valid_limit_down = self._numeric(market.get("limit_down"))
            high, valid_high = self._numeric(market.get("high"))
            low, valid_low = self._numeric(market.get("low"))
            if not all((valid_price, valid_limit_up, valid_limit_down, valid_high, valid_low)):
                reason = "no_market_data"
            elif price <= 0 or high <= 0 or low <= 0 or limit_up <= 0 or limit_down <= 0:
                reason = "no_market_data"
            elif limit_down > limit_up or low > high:
                reason = "price"
            elif row["side"] == "buy" and price > float(row["limit_price"]):
                reason = "price"
            elif row["side"] == "sell" and price < float(row["limit_price"]):
                reason = "price"
            elif row["side"] == "buy" and not self._within_limit(price, limit_up, config, "up"):
                reason = "limit_up"
            elif row["side"] == "sell" and not self._within_limit(price, limit_down, config, "down"):
                reason = "limit_down"
        if reason != "none":
            filled = 0
            price = 0.0
        else:
            filled = requested
            price = (
                float(execution_market[instrument]["open"])
                if config["price_policy"]["order_pricing_basis"] == "execution_open_replay"
                else float(row["limit_price"])
            )
        gross = filled * price
        fee = self._event_fee(config, row["side"], gross) if filled > 0 else 0.0
        if reason == "none" and row["side"] == "buy" and available_cash is not None and gross + fee > available_cash + 1e-9:
            reason = "insufficient_cash"
            filled = 0
            gross = 0.0
            fee = 0.0
        if reason == "none" and row["side"] == "sell" and gross - fee < 0:
            reason = "price"
            filled = 0
            gross = 0.0
            fee = 0.0
        net = 0.0
        if filled > 0:
            net = -(gross + fee) if row["side"] == "buy" else gross - fee
        order_state = "filled" if reason == "none" else "rejected"
        return {
            "instrument": instrument,
            "side": str(row["side"]),
            "order_state": order_state,
            "reject_reason": reason if reason != "none" else "none",
            "filled_quantity": int(filled),
            "requested_quantity": int(requested),
            "price": float(price),
            "gross_amount": float(gross),
            "fee_amount": float(fee),
            "net_amount": float(net),
            "plan_sequence": int(row["plan_sequence"]),
            "decision_date": pd.Timestamp(config["decision_date"]),
            "execution_date": pd.Timestamp(config["execution_date"]),
        }

    def _within_limit(self, price: float, bound: float, config: Mapping[str, Any], side: str) -> bool:
        policy = config["price_policy"]
        allowance = float(policy["limit_tolerance_absolute"]) + float(policy["limit_tolerance_relative"]) * bound
        if side == "up":
            return price < bound + allowance
        return price > bound - allowance

    def _event_fee(self, config: Mapping[str, Any], side: str, gross: float) -> float:
        fees = config["fee_policy"]
        precision = int(fees["currency_precision"])
        fee = gross * float(fees[f"{side}_commission_rate"]) + gross * float(fees["transfer_fee_rate"])
        if side == "sell":
            fee += gross * float(fees["sell_stamp_tax_rate"])
        fee = max(fee, float(fees["minimum_commission"]))
        return round(math.floor(fee * (10 ** precision) + 0.5) / (10 ** precision), precision)

    def _aggregate_reconciliation(
        self,
        config: Mapping[str, Any],
        target_weights: pd.DataFrame,
        plan_frame: pd.DataFrame,
        result_frame: pd.DataFrame,
        decision_nav: float,
    ) -> dict[str, Any]:
        target = target_weights.copy()
        target["instrument"] = target["instrument"].map(str)
        plan = plan_frame.astype({"instrument": "string", "plan_sequence": "int64", "target_delta_shares": "int64"})
        plan_by_instrument = {str(row["instrument"]): row for _, row in plan.iterrows() if int(row["target_delta_shares"]) != 0}
        for row in target.to_dict("records"):
            instrument = str(row["instrument"])
            if float(row["weight"]) > 0 and instrument not in plan_by_instrument:
                raise ContractError(f"order plan is missing target instrument {instrument}")
        events_by_sequence = {int(row["plan_sequence"]): row for _, row in result_frame.iterrows()}
        unfilled: dict[str, int] = defaultdict(int)
        for _, row in plan.iterrows():
            delta = int(row["target_delta_shares"])
            if delta == 0:
                continue
            event = events_by_sequence[int(row["plan_sequence"])]
            filled = int(event["filled_quantity"])
            actual_delta = filled if row["side"] == "buy" else -filled
            residual = abs(delta - actual_delta)
            if residual == 0:
                continue
            if filled == 0:
                reason = str(event["reject_reason"]) if event["reject_reason"] != "none" else "lot_size"
            elif filled < int(row["requested_quantity"]):
                reason = str(event["reject_reason"]) if event["reject_reason"] != "none" else "lot_size"
            else:
                reason = "lot_size"
            unfilled[reason] += residual
        rejected: dict[str, int] = defaultdict(int)
        for _, event in result_frame.iterrows():
            if event["order_state"] == "rejected":
                rejected[str(event["reject_reason"])] += int(event["requested_quantity"])
        planned_buy = plan.loc[plan["side"] == "buy", "requested_quantity"].sum()
        planned_sell = plan.loc[plan["side"] == "sell", "requested_quantity"].sum()
        planned_buy_notional = (
            plan.loc[plan["side"] == "buy", "requested_quantity"]
            .mul(plan.loc[plan["side"] == "buy", "limit_price"])
            .sum()
        )
        planned_sell_notional = (
            plan.loc[plan["side"] == "sell", "requested_quantity"]
            .mul(plan.loc[plan["side"] == "sell", "limit_price"])
            .sum()
        )
        return {
            "target_instrument_count": int(len(target)),
            "target_total_stock_weight": float(target["weight"].map(float).sum()),
            "planned_buy_quantity": int(planned_buy),
            "planned_sell_quantity": int(planned_sell),
            "planned_buy_notional": float(planned_buy_notional),
            "planned_sell_notional": float(planned_sell_notional),
            "filled_buy_quantity": int(result_frame.loc[result_frame["side"] == "buy", "filled_quantity"].sum()),
            "filled_sell_quantity": int(result_frame.loc[result_frame["side"] == "sell", "filled_quantity"].sum()),
            "filled_buy_gross_amount": float(result_frame.loc[result_frame["side"] == "buy", "gross_amount"].sum()),
            "filled_sell_gross_amount": float(result_frame.loc[result_frame["side"] == "sell", "gross_amount"].sum()),
            "total_fee_amount": float(result_frame["fee_amount"].sum()),
            "net_cash_movement": float(result_frame["net_amount"].sum()),
            "rejected_quantity_by_reason": dict(sorted(rejected.items())),
            "unfilled_target_quantity_by_reason": dict(sorted(unfilled.items())),
        }

    def _holdings_by_instrument(self, input_holdings: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        holdings = {str(row["instrument"]): dict(row) for row in input_holdings}
        if len(holdings) != len(input_holdings):
            raise ContractError("duplicate input holdings")
        for holding in holdings.values():
            quantity = int(holding["quantity"])
            average_cost = float(holding["average_cost"])
            buy_locked = int(holding["buy_locked_quantity"])
            if quantity < 0 or buy_locked < 0 or buy_locked > quantity or average_cost < 0:
                raise ContractError("input holdings contain invalid quantities")
        return holdings

    def _sellable_quantities(self, config: Mapping[str, Any], input_holdings: list[Mapping[str, Any]]) -> dict[str, int]:
        same_session = config["state_mode"] == "continuation" and config["input_state_binding"]["as_of_date"] >= config["execution_date"]
        return {
            str(row["instrument"]): int(row["quantity"]) - (int(row["buy_locked_quantity"]) if same_session else 0)
            for row in input_holdings
        }

    def _decision_nav(
        self,
        opening_cash: float,
        holdings: Mapping[str, Mapping[str, Any]],
        decision_prices: Mapping[str, Mapping[str, float]],
    ) -> float:
        return self._portfolio_value(opening_cash, holdings, decision_prices, "decision", require_positive_price=True)

    def _portfolio_value(
        self,
        cash: float,
        holdings: Mapping[str, Mapping[str, Any]],
        prices: Mapping[str, Mapping[str, Any]],
        basis: str,
        *,
        require_positive_price: bool,
    ) -> float:
        value = float(cash)
        for instrument, holding in holdings.items():
            quantity = int(holding["quantity"])
            if quantity <= 0:
                continue
            market = prices.get(instrument)
            close, valid = self._numeric((market or {}).get("close"))
            if not valid or close <= 0:
                if require_positive_price:
                    raise ContractError(f"missing {basis} close for surviving holding {instrument}")
                value += 0.0
            else:
                value += quantity * close
        return value

    def _closing_holdings(
        self, holdings: Mapping[str, Mapping[str, Any]], events: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        closing = {key: dict(value) for key, value in holdings.items()}
        for event in events:
            instrument = str(event["instrument"])
            if instrument not in closing:
                if int(event["filled_quantity"]) == 0:
                    continue
                if event["side"] != "buy":
                    raise ContractError(f"execution event references unknown holding {instrument}")
                closing[instrument] = {
                    "instrument": instrument, "quantity": 0, "average_cost": 0.0,
                    "buy_locked_quantity": 0,
                }
            if event["side"] == "buy":
                closing[instrument]["quantity"] += int(event["filled_quantity"])
            else:
                closing[instrument]["quantity"] -= int(event["filled_quantity"])
            if int(closing[instrument]["quantity"]) < 0:
                raise ContractError(f"execution sells more than held quantity for {instrument}")
        return {key: value for key, value in closing.items() if int(value["quantity"]) > 0}

    def _manifest(
        self,
        config: Mapping[str, Any],
        plan_manifest: Mapping[str, Any],
        frame: pd.DataFrame,
        aggregate: Mapping[str, Any],
        *,
        opening_cash: float,
        closing_cash: float,
        opening_portfolio_value: float,
        closing_portfolio_value: float,
    ) -> dict[str, Any]:
        records = self._canonical_records(frame)
        return {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "execution_id": config["execution_id"],
            "state_mode": config["state_mode"],
            "decision_date": config["decision_date"],
            "execution_date": config["execution_date"],
            "order_plan_generation_id": plan_manifest["generation_id"],
            "data_file": "data.parquet",
            "data_checksum_sha256": "0" * 64,
            "columns": self.COLUMNS,
            "dtypes": self.DTYPES,
            "row_count": int(len(frame)),
            "key_uniqueness": ["plan_sequence"],
            "logical_fingerprint": sha256_json(records),
            "serialization_profile_id": "parquet-v1",
            "target_weights_binding": config["target_weights_binding"],
            "input_state_binding": config["input_state_binding"],
            "initial_state_provenance_sha256": (
                config["initial_state"]["provenance_sha256"]
                if config["state_mode"] == "initial"
                else None
            ),
            "market_data_binding": {
                "family": config["market_data_binding"]["dataset_family"],
                "generation_id": config["market_data_binding"]["generation_id"],
                "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
            },
            "calendar_binding": self._binding(config["calendar_binding"]),
            "suspension_binding": self._binding(config["suspension_binding"]),
            "corporate_action_binding": self._binding(config["corporate_action_binding"]),
            "risk_decision_binding": self._binding(config["risk_decision_binding"]),
            "aggregate_reconciliation": dict(aggregate),
            "reconciliation_tolerance": 1e-6,
            "opening_cash": float(opening_cash),
            "closing_cash": float(closing_cash),
            "opening_portfolio_value": float(opening_portfolio_value),
            "closing_portfolio_value": float(closing_portfolio_value),
            "run_id": config["run_id"],
            "created_at": config["created_at"],
            "quality_report_checksum_sha256": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
            "generation_id": "0" * 64,
        }

    def _canonical_records(self, frame: pd.DataFrame) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for row in frame.sort_values("plan_sequence", kind="mergesort").to_dict("records"):
            normalized: dict[str, Any] = {}
            for key, value in row.items():
                if pd.isna(value):
                    normalized[key] = None
                elif isinstance(value, pd.Timestamp):
                    normalized[key] = value.isoformat()
                else:
                    normalized[key] = value
            records.append(normalized)
        return records

    def _binding(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "family": value["family"],
            "generation_id": value["generation_id"],
            "manifest_digest_sha256": value["manifest_digest_sha256"],
        }

    @staticmethod
    def _numeric(value: Any) -> tuple[float, bool]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0, False
        return number, math.isfinite(number)

    @staticmethod
    def _positive_float(value: Any) -> bool:
        number, valid = PaperExecutionEngine._numeric(value)
        return valid and number > 0
