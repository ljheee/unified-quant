from __future__ import annotations

import math
from typing import Any, Mapping

import pandas as pd

from ..contracts.model_layer import ModelContractLoader
from ..errors import ContractError
from .stores import ExecutionConfigStore


class PaperOrderPlanner:
    """Create a deterministic all-or-none paper order plan."""

    COLUMNS = [
        "instrument", "side", "requested_quantity", "limit_price", "order_type",
        "reason", "target_delta_shares", "sellable_quantity", "previous_quantity",
        "target_quantity", "order_state", "plan_sequence",
    ]

    DTYPES = {
        "instrument": "string", "side": "string", "requested_quantity": "int64",
        "limit_price": "float64", "order_type": "string", "reason": "string",
        "target_delta_shares": "int64", "sellable_quantity": "int64",
        "previous_quantity": "int64", "target_quantity": "int64",
        "order_state": "string", "plan_sequence": "int64",
    }

    def plan(
        self,
        config: Mapping[str, Any],
        target_weights: pd.DataFrame,
        input_holdings: list[Mapping[str, Any]] | None,
        prices: Mapping[str, Mapping[str, float]],
        *,
        opening_cash: float,
    ) -> tuple[pd.DataFrame, list[int]]:
        ModelContractLoader.validate("execution_config", config)
        required = {"instrument", "weight"}
        if set(target_weights.columns) < required:
            raise ContractError("target weights must contain instrument and weight")
        if target_weights["instrument"].duplicated().any():
            raise ContractError("duplicate target instruments")
        if not target_weights["weight"].map(math.isfinite).all() or (target_weights["weight"] < 0).any():
            raise ContractError("target weights contain invalid values")
        if not math.isfinite(float(opening_cash)) or opening_cash < 0:
            raise ContractError("opening cash must be finite and non-negative")
        quantity_policy = config["quantity_policy"]
        board_lot = int(quantity_policy["board_lot"])
        if quantity_policy["sell_before_buy"] is not True or quantity_policy["enforce_t1"] is not True:
            raise ContractError("paper planner requires sell-before-buy and enforced T+1")
        if quantity_policy["partial_fills"] is not False:
            raise ContractError("paper planner does not support partial fills")
        if board_lot <= 0 or int(quantity_policy["minimum_order_quantity"]) != board_lot or int(config["lot_size"]) != board_lot:
            raise ContractError("paper board lot settings must reconcile")
        if config["state_mode"] == "initial":
            if input_holdings is not None:
                raise ContractError("initial execution config must use initial state holdings")
            state = config["initial_state"]
            if opening_cash != float(state["cash"]):
                raise ContractError("opening cash does not match initial state")
            holdings = [dict(row) for row in state["holdings"]]
        else:
            if input_holdings is None:
                raise ContractError("continuation execution requires input holdings")
            holdings = [dict(row) for row in input_holdings]
        by_instrument = {row["instrument"]: row for row in holdings}
        if len(by_instrument) != len(holdings):
            raise ContractError("duplicate initial holdings")
        for instrument, holding in by_instrument.items():
            quantity = int(holding["quantity"])
            buy_locked = int(holding["buy_locked_quantity"])
            if quantity < 0 or buy_locked < 0 or buy_locked > quantity:
                raise ContractError("initial holdings contain invalid quantities")
            if config["state_mode"] == "continuation":
                as_of_date = str(config["input_state_binding"]["as_of_date"])
                if as_of_date >= config["execution_date"]:
                    holding["sellable_quantity"] = quantity - buy_locked
                else:
                    holding["sellable_quantity"] = quantity
            elif int(holding["sellable_quantity"]) != quantity - buy_locked:
                raise ContractError("initial holding sellable quantity does not reconcile")
        instruments = sorted(set(target_weights["instrument"].map(str)) | set(by_instrument))
        for instrument in instruments:
            if instrument not in prices:
                raise ContractError(f"missing decision close for {instrument}")
            close = prices[instrument].get("close")
            if close is None or not math.isfinite(float(close)) or close <= 0:
                raise ContractError(f"invalid decision close for {instrument}")
        fees = config["fee_policy"]
        precision = int(fees["currency_precision"])

        def round_fee(value: float) -> float:
            return round(math.floor(value * (10 ** precision) + 0.5) / (10 ** precision), precision)

        def event_fee(side: str, gross: float) -> float:
            rate = fees["sell_commission_rate"] if side == "sell" else fees["buy_commission_rate"]
            fee = gross * rate + gross * fees["transfer_fee_rate"]
            if side == "sell":
                fee += gross * fees["sell_stamp_tax_rate"]
            fee = max(fee, float(fees["minimum_commission"]))
            return round_fee(fee)

        records: list[dict[str, Any]] = []
        sequence = 1
        reject_insufficient_cash = config["quantity_policy"]["insufficient_cash_policy"] == "reject"
        for instrument in sorted(instruments):
            market = prices[instrument]
            close = float(market["close"])
            basis = config["price_policy"]["order_pricing_basis"]
            limit_price = close if basis == "decision_close_estimate" else market.get("open")
            if limit_price is None or not math.isfinite(float(limit_price)) or limit_price <= 0:
                raise ContractError(f"missing order limit price for {instrument}")
            limit_price = float(limit_price)
            holding = by_instrument.get(instrument)
            previous = int(holding["quantity"]) if holding else 0
            target_row = target_weights.loc[target_weights["instrument"] == instrument]
            target_weight = float(target_row.iloc[0]["weight"]) if not target_row.empty else 0.0
            decision_nav = float(opening_cash) + sum(
                int(item["quantity"]) * float(prices[item["instrument"]]["close"])
                for item in by_instrument.values()
            )
            target_quantity = int(math.floor(target_weight * decision_nav / close))
            target_delta = target_quantity - previous
            if target_delta == 0:
                continue
            side = "sell" if target_delta < 0 else "buy"
            sellable = int(holding["sellable_quantity"]) if holding else 0
            requested = abs(target_delta) // board_lot * board_lot
            if side == "sell":
                requested = min(requested, max(sellable, 0))
                remaining = previous - requested
                if config["quantity_policy"]["minimum_holding_rule"] == "preserve_one_lot":
                    if 0 < remaining < board_lot:
                        requested = max(previous - board_lot, 0)
                if requested > 0:
                    gross = requested * limit_price
                    fee = event_fee(side, gross)
                    if gross - fee < 0:
                        raise ContractError("sell fees exceed planned sell proceeds")
            reason = "target_rebalance"
            if requested == 0 and side == "buy" and target_delta > 0:
                reason = "cash_residual"
            records.append({
                "instrument": instrument, "side": side, "requested_quantity": int(requested),
                "limit_price": limit_price, "order_type": "limit",
                "reason": reason, "target_delta_shares": int(target_delta),
                "sellable_quantity": int(sellable), "previous_quantity": int(previous),
                "target_quantity": int(target_quantity), "order_state": "planned",
                "plan_sequence": sequence,
            })
            sequence += 1
        ordered = sorted(records, key=lambda row: (0 if row["side"] == "sell" else 1, row["plan_sequence"]))
        for new_sequence, row in enumerate(ordered, start=1):
            row["plan_sequence"] = new_sequence

        available_cash = float(opening_cash)
        for row in (item for item in ordered if item["side"] == "sell"):
            gross = int(row["requested_quantity"]) * float(row["limit_price"])
            available_cash += gross - event_fee("sell", gross)
        buys = [row for row in ordered if row["side"] == "buy"]
        for row in buys:
            gross = int(row["requested_quantity"]) * float(row["limit_price"])
            fee = event_fee("buy", gross)
            if gross + fee > available_cash:
                if config["quantity_policy"]["insufficient_cash_policy"] == "reject":
                    raise ContractError(f"insufficient cash for buy {row['instrument']}")
                lot = board_lot
                while int(row["requested_quantity"]) > 0 and gross + fee > available_cash:
                    row["requested_quantity"] = int(row["requested_quantity"]) - lot
                    gross = int(row["requested_quantity"]) * float(row["limit_price"])
                    fee = event_fee("buy", gross)
                if int(row["requested_quantity"]) <= 0:
                    row["reason"] = "cash_residual"
                    continue
            available_cash -= gross + fee
        for new_sequence, row in enumerate(ordered, start=1):
            row["plan_sequence"] = new_sequence
        cash_residual_sequence = [int(row["plan_sequence"]) for row in ordered if row["side"] == "buy"]
        frame = pd.DataFrame(ordered, columns=self.COLUMNS).astype(self.DTYPES)
        return frame, cash_residual_sequence
