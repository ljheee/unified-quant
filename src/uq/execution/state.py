"""Deterministic evolution and reconciliation for paper portfolio state."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import pandas as pd

from ..contracts.model_layer import (
    ModelContractLoader,
    model_manifest_identities,
    paper_execution_identities,
    sha256_json,
)
from ..errors import ContractError


class PaperPortfolioStateBuilder:
    """Build a next paper portfolio state from governed result lineage."""

    COLUMNS = [
        "instrument", "quantity", "average_cost", "buy_locked_quantity",
        "sellable_quantity", "market_value",
    ]
    DTYPES = {
        "instrument": "string", "quantity": "int64", "average_cost": "float64",
        "buy_locked_quantity": "int64", "sellable_quantity": "int64",
        "market_value": "float64",
    }

    def build(
        self,
        config: Mapping[str, Any],
        *,
        result_manifest: Mapping[str, Any],
        result_frame: pd.DataFrame,
        plan_manifest: Mapping[str, Any],
        plan_frame: pd.DataFrame,
        target_weights_manifest: Mapping[str, Any],
        target_weights_frame: pd.DataFrame,
        execution_market: Mapping[str, Mapping[str, Any]],
        decision_prices: Mapping[str, Mapping[str, Any]],
        input_holdings: list[Mapping[str, Any]],
        opening_cash: float,
        previous_state_manifest: Mapping[str, Any] | None = None,
        previous_state_frame: pd.DataFrame | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        self._validate_config(config, previous_state_manifest)
        self._validate_lineage(
            config,
            result_manifest=result_manifest,
            plan_manifest=plan_manifest,
            target_weights_manifest=target_weights_manifest,
            previous_state_manifest=previous_state_manifest,
        )
        expected_cash = (
            float(previous_state_manifest["cash"])
            if previous_state_manifest is not None
            else float(config["initial_state"]["cash"])
        )
        if not math.isfinite(expected_cash) or expected_cash < 0 or not math.isclose(
            float(opening_cash), expected_cash, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ContractError("execution opening cash does not match governed input state")
        net_cash = float(result_frame["net_amount"].sum())
        closing_cash = float(result_manifest["closing_cash"])
        if not math.isclose(opening_cash + net_cash, closing_cash, rel_tol=0.0, abs_tol=1e-9):
            raise ContractError("execution result cash does not reconcile")
        if closing_cash < -1e-9:
            raise ContractError("paper state cash cannot be negative")
        self._validate_target_frame(
            result_manifest, target_weights_manifest, target_weights_frame
        )
        self._validate_plan_frame(plan_manifest, plan_frame)
        self._validate_result_payload(result_manifest, result_frame, plan_frame)
        self._validate_plan_mapping(plan_frame, result_frame)
        self._validate_reconciliation(plan_frame, result_frame, result_manifest)
        same_session = (
            config["state_mode"] == "continuation"
            and config["input_state_binding"]["as_of_date"] >= config["execution_date"]
        )
        if previous_state_manifest is not None:
            self._validate_previous_state_frame(
                previous_state_manifest,
                previous_state_frame,
                input_holdings,
            )
        holdings = self._closing_holdings(
            input_holdings,
            result_frame,
            same_session_prior_state=same_session,
            previous_state_frame=previous_state_frame,
            previous_state_manifest=previous_state_manifest,
        )
        frame = self._state_frame(closing_cash, holdings, execution_market)
        self._validate_valuation(
            result_manifest,
            opening_cash,
            input_holdings,
            decision_prices,
            frame,
        )
        manifest = self._manifest(
            config,
            frame,
            result_manifest=result_manifest,
            previous_state_manifest=previous_state_manifest,
        )
        return frame, manifest

    def _validate_config(
        self,
        config: Mapping[str, Any],
        previous_state_manifest: Mapping[str, Any] | None,
    ) -> None:
        ModelContractLoader.validate("execution_config", dict(config))
        if config["mode"] != "paper" or config["market"] != "cn_a":
            raise ContractError("paper state builder only supports cn_a paper mode")
        if config["state_mode"] == "continuation" and previous_state_manifest is None:
            raise ContractError("continuation paper state requires previous state")

    def _validate_lineage(
        self,
        config: Mapping[str, Any],
        *,
        result_manifest: Mapping[str, Any],
        plan_manifest: Mapping[str, Any],
        target_weights_manifest: Mapping[str, Any],
        previous_state_manifest: Mapping[str, Any] | None,
    ) -> None:
        ModelContractLoader.validate("execution_result", dict(result_manifest))
        ModelContractLoader.validate("order_plan", dict(plan_manifest))
        target_generation, target_digest = model_manifest_identities(
            target_weights_manifest, schema_name="target_weights", exclude_fields=set()
        )
        if (
            target_weights_manifest["generation_id"] != target_generation
            or target_weights_manifest["manifest_digest_sha256"] != target_digest
        ):
            raise ContractError("target weights manifest identity mismatch")
        if config["target_weights_binding"] != {
            "family": "target_weights_v1",
            "generation_id": target_weights_manifest["generation_id"],
            "manifest_digest_sha256": target_weights_manifest["manifest_digest_sha256"],
        }:
            raise ContractError("target weights do not bind execution config")
        for manifest, family in (
            (result_manifest, "execution_result"),
            (plan_manifest, "order_plan"),
        ):
            generation, digest = paper_execution_identities(manifest, schema_name=family)
            if (
                manifest["generation_id"] != generation
                or manifest["manifest_digest_sha256"] != digest
            ):
                raise ContractError(f"{family} manifest identity mismatch")
        expected_target_binding = {
            "family": "target_weights_v1",
            "generation_id": target_weights_manifest["generation_id"],
            "manifest_digest_sha256": target_weights_manifest["manifest_digest_sha256"],
        }
        if config["target_weights_binding"] != expected_target_binding:
            raise ContractError(
                f"target weights do not bind execution config: {config['target_weights_binding']} != {expected_target_binding}"
            )
        if result_manifest["order_plan_generation_id"] != plan_manifest["generation_id"]:
            raise ContractError("execution result does not bind order plan")
        if plan_manifest["target_weights_binding"] != config["target_weights_binding"]:
            raise ContractError("order plan does not bind execution target weights")
        shared_fields = (
            "decision_date", "execution_date", "execution_id", "state_mode",
            "target_weights_binding", "input_state_binding",
            "initial_state_provenance_sha256", "market_data_binding",
            "calendar_binding", "suspension_binding", "corporate_action_binding",
            "risk_decision_binding",
        )
        for field in shared_fields:
            if result_manifest[field] != plan_manifest[field]:
                raise ContractError(f"order plan and result {field} mismatch")
        if previous_state_manifest is None:
            if config["state_mode"] != "initial":
                raise ContractError("continuation paper state requires prior state")
            return
        ModelContractLoader.validate("paper_portfolio_state", dict(previous_state_manifest))
        state_generation, state_digest = paper_execution_identities(
            previous_state_manifest, schema_name="paper_portfolio_state"
        )
        if (
            previous_state_manifest["generation_id"] != state_generation
            or previous_state_manifest["manifest_digest_sha256"] != state_digest
        ):
            raise ContractError("previous paper state manifest identity mismatch")
        expected_input_binding = {
            "family": "paper_portfolio_state_v1",
            "generation_id": previous_state_manifest["generation_id"],
            "manifest_digest_sha256": previous_state_manifest["manifest_digest_sha256"],
            "as_of_date": previous_state_manifest["state_date"],
        }
        if dict(config["input_state_binding"]) != expected_input_binding:
            raise ContractError("execution config does not bind previous paper state")
        if plan_manifest["input_state_binding"] != expected_input_binding:
            raise ContractError("order plan does not bind previous paper state")


    def _validate_target_frame(
        self,
        result_manifest: Mapping[str, Any],
        target_weights_manifest: Mapping[str, Any],
        target_weights_frame: pd.DataFrame,
    ) -> None:
        if int(target_weights_manifest["row_count"]) != len(target_weights_frame):
            raise ContractError("target weights row count mismatch")
        if target_weights_frame["instrument"].duplicated().any():
            raise ContractError("duplicate target instruments")
        if list(target_weights_frame.columns) != list(target_weights_manifest["columns"]):
            raise ContractError("target weights column mismatch")
        for key, expected_dtype in target_weights_manifest["dtypes"].items():
            if not self._dtype_matches(target_weights_frame[key].dtype, expected_dtype):
                raise ContractError(f"target weights dtype mismatch for {key}")
        if target_weights_frame["instrument"].tolist() != sorted(
            target_weights_frame["instrument"].tolist()
        ):
            raise ContractError("target weights payload ordering mismatch")
        if not target_weights_frame["weight"].map(math.isfinite).all() or (
            target_weights_frame["weight"] < 0
        ).any():
            raise ContractError("target weights contain an invalid weight")
        aggregate = result_manifest["aggregate_reconciliation"]
        if int(aggregate["target_instrument_count"]) != len(target_weights_frame):
            raise ContractError("target instrument count does not reconcile")
        if not math.isclose(
            float(aggregate["target_total_stock_weight"]),
            float(target_weights_frame["weight"].sum()),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ContractError("target weight total does not reconcile")


    @staticmethod
    def _validate_reconciliation(
        plan_frame: pd.DataFrame,
        result_frame: pd.DataFrame,
        result_manifest: Mapping[str, Any],
    ) -> None:
        from collections import defaultdict

        aggregate = result_manifest["aggregate_reconciliation"]
        buys = plan_frame[plan_frame["side"] == "buy"]
        sells = plan_frame[plan_frame["side"] == "sell"]
        result_buys = result_frame[result_frame["side"] == "buy"]
        result_sells = result_frame[result_frame["side"] == "sell"]
        expected = {
            "planned_buy_quantity": int(buys["requested_quantity"].sum()),
            "planned_sell_quantity": int(sells["requested_quantity"].sum()),
            "planned_buy_notional": float((buys["requested_quantity"] * buys["limit_price"]).sum()),
            "planned_sell_notional": float((sells["requested_quantity"] * sells["limit_price"]).sum()),
            "filled_buy_quantity": int(result_buys["filled_quantity"].sum()),
            "filled_sell_quantity": int(result_sells["filled_quantity"].sum()),
            "filled_buy_gross_amount": float(result_buys["gross_amount"].sum()),
            "filled_sell_gross_amount": float(result_sells["gross_amount"].sum()),
            "total_fee_amount": float(result_frame["fee_amount"].sum()),
            "net_cash_movement": float(result_frame["net_amount"].sum()),
        }
        for field, value in expected.items():
            if not math.isclose(float(aggregate[field]), float(value), rel_tol=0.0, abs_tol=1e-9):
                raise ContractError(f"execution result aggregate does not reconcile: {field}")
        rejected: dict[str, int] = defaultdict(int)
        for event in result_frame.to_dict(orient="records"):
            if event["order_state"] == "rejected":
                rejected[str(event["reject_reason"])] += int(event["requested_quantity"])
        if aggregate["rejected_quantity_by_reason"] != dict(sorted(rejected.items())):
            raise ContractError("rejected quantities do not reconcile")
        result_by_sequence = result_frame.set_index("plan_sequence", drop=False)
        unfilled: dict[str, int] = defaultdict(int)
        for event in plan_frame.to_dict(orient="records"):
            delta = int(event["target_delta_shares"])
            if delta == 0:
                continue
            result_event = result_by_sequence.loc[int(event["plan_sequence"])]
            filled = int(result_event["filled_quantity"])
            actual_delta = filled if event["side"] == "buy" else -filled
            residual = abs(delta - actual_delta)
            if residual == 0:
                continue
            if filled == 0:
                reason = str(result_event["reject_reason"])
            elif filled < int(event["requested_quantity"]):
                reason = str(result_event["reject_reason"])
            else:
                reason = "lot_size"
            unfilled[reason] += residual
        if aggregate["unfilled_target_quantity_by_reason"] != dict(sorted(unfilled.items())):
            raise ContractError("unfilled target quantities do not reconcile")

    @staticmethod
    def _validate_plan_mapping(
        plan_frame: pd.DataFrame,
        result_frame: pd.DataFrame,
    ) -> None:
        plan = plan_frame.set_index("plan_sequence", drop=False)
        result = result_frame.set_index("plan_sequence", drop=False)
        if set(plan.index) != set(result.index):
            raise ContractError("execution result does not reconcile to order plan")
        for sequence in plan.index:
            if (
                str(plan.loc[sequence, "instrument"]) != str(result.loc[sequence, "instrument"])
                or str(plan.loc[sequence, "side"]) != str(result.loc[sequence, "side"])
            ):
                    raise ContractError("execution result plan mapping mismatch")

    def _validate_previous_state_frame(
        self,
        previous_state_manifest: Mapping[str, Any],
        previous_state_frame: pd.DataFrame | None,
        input_holdings: list[Mapping[str, Any]],
    ) -> None:
        if previous_state_frame is None:
            raise ContractError("continuation paper state requires prior state payload")
        if int(previous_state_manifest["row_count"]) != len(previous_state_frame):
            raise ContractError("previous paper state row count mismatch")
        if list(previous_state_frame.columns) != list(previous_state_manifest["columns"]):
            raise ContractError("previous paper state column mismatch")
        for key, expected_dtype in previous_state_manifest["dtypes"].items():
            if not self._dtype_matches(previous_state_frame[key].dtype, expected_dtype):
                raise ContractError(f"previous paper state dtype mismatch for {key}")
        if previous_state_frame["instrument"].duplicated().any():
            raise ContractError("previous paper state contains duplicate instruments")
        if previous_state_frame["instrument"].tolist() != sorted(
            previous_state_frame["instrument"].tolist()
        ):
            raise ContractError("previous paper state payload ordering mismatch")
        for row in previous_state_frame.to_dict(orient="records"):
            quantity = int(row["quantity"])
            buy_locked = int(row["buy_locked_quantity"])
            average_cost = float(row["average_cost"])
            market_value = float(row["market_value"])
            if (
                quantity <= 0
                or buy_locked < 0
                or buy_locked > quantity
                or not math.isfinite(average_cost)
                or average_cost < 0
                or not math.isfinite(market_value)
                or market_value <= 0
                or int(row["sellable_quantity"]) != quantity - buy_locked
            ):
                raise ContractError("previous paper state contains an invalid holding")
        if int(previous_state_manifest["row_count"]) > 0:
            self._validate_input_holdings_match_frame(
                previous_state_frame, input_holdings
            )

    def _validate_input_holdings_match_frame(
        self,
        previous_state_frame: pd.DataFrame,
        input_holdings: list[Mapping[str, Any]],
    ) -> None:
        expected = previous_state_frame[
            ["instrument", "quantity", "average_cost", "buy_locked_quantity"]
        ].sort_values("instrument").to_dict(orient="records")
        actual = [
            {
                "instrument": str(row["instrument"]),
                "quantity": int(row["quantity"]),
                "average_cost": float(row["average_cost"]),
                "buy_locked_quantity": int(row["buy_locked_quantity"]),
            }
            for row in sorted(input_holdings, key=lambda row: str(row["instrument"]))
            if int(row["quantity"]) > 0
        ]
        if actual != expected:
            raise ContractError("input holdings do not match previous state payload")

    def _validate_plan_frame(
        self,
        plan_manifest: Mapping[str, Any],
        plan_frame: pd.DataFrame,
    ) -> None:
        if int(plan_manifest["row_count"]) != len(plan_frame):
            raise ContractError("order plan row count mismatch")
        if list(plan_frame.columns) != list(plan_manifest["columns"]):
            raise ContractError("order plan column mismatch")
        for key, expected_dtype in plan_manifest["dtypes"].items():
            if not self._dtype_matches(plan_frame[key].dtype, expected_dtype):
                raise ContractError(f"order plan dtype mismatch for {key}")
        if plan_frame["plan_sequence"].duplicated().any():
            raise ContractError("order plan sequence is not unique")
        if plan_frame["plan_sequence"].tolist() != list(range(1, len(plan_frame) + 1)):
            raise ContractError("order plan sequence is not contiguous")
        sides = plan_frame["side"].tolist()
        if sides != sorted(sides, key=lambda side: 0 if side == "sell" else 1):
            raise ContractError("order plan does not sequence sells before buys")
        for row in plan_frame.to_dict(orient="records"):
            instrument = str(row["instrument"])
            if row["side"] == "sell" and int(row["requested_quantity"]) > int(row["sellable_quantity"]):
                raise ContractError(f"order plan sell exceeds sellable quantity for {instrument}")

    @staticmethod
    def _dtype_matches(actual: Any, expected: str) -> bool:
        return str(actual) == expected or (expected == "string" and str(actual) == "object")

    @staticmethod
    def _validate_result_payload(
        result_manifest: Mapping[str, Any],
        result_frame: pd.DataFrame,
        plan_frame: pd.DataFrame,
    ) -> None:
        if int(result_manifest["row_count"]) != len(result_frame):
            raise ContractError("execution result row count mismatch")
        if result_frame["plan_sequence"].duplicated().any():
            raise ContractError("execution result plan sequence is not unique")
        if set(result_frame["plan_sequence"]) != set(plan_frame["plan_sequence"]):
            raise ContractError("execution result does not reconcile to order plan")
        plan_instruments = set(plan_frame["instrument"].map(str))
        if not result_frame["instrument"].map(str).isin(plan_instruments).all():
            raise ContractError("execution result references unknown plan instrument")
        for column in ("filled_quantity", "net_amount", "fee_amount", "price"):
            if not result_frame[column].map(math.isfinite).all():
                raise ContractError(f"execution result contains non-finite {column}")
        if (result_frame["filled_quantity"] < 0).any():
            raise ContractError("execution result contains negative filled quantity")


    def _validate_valuation(
        self,
        result_manifest: Mapping[str, Any],
        opening_cash: float,
        input_holdings: list[Mapping[str, Any]],
        decision_prices: Mapping[str, Mapping[str, Any]],
        state_frame: pd.DataFrame,
    ) -> None:
        opening_value = float(opening_cash)
        for row in input_holdings:
            instrument = str(row["instrument"])
            quantity = int(row["quantity"])
            if quantity <= 0:
                continue
            close = float((decision_prices.get(instrument) or {}).get("close") or 0.0)
            if not math.isfinite(close) or close <= 0:
                raise ContractError(f"missing decision close for surviving holding {instrument}")
            opening_value += quantity * close
        if not math.isclose(
            opening_value,
            float(result_manifest["opening_portfolio_value"]),
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ContractError("opening portfolio value does not reconcile")
        closing_value = float(result_manifest["closing_cash"]) + float(
            state_frame["market_value"].sum()
        )
        if not math.isclose(
            closing_value,
            float(result_manifest["closing_portfolio_value"]),
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ContractError("closing portfolio value does not reconcile")

    def _closing_holdings(
        self,
        input_holdings: list[Mapping[str, Any]],
        result_frame: pd.DataFrame,
        *,
        same_session_prior_state: bool,
        previous_state_frame: pd.DataFrame | None,
        previous_state_manifest: Mapping[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        holdings = {str(row["instrument"]): dict(row) for row in input_holdings}
        if len(holdings) != len(input_holdings):
            raise ContractError("duplicate input holdings")
        for holding in holdings.values():
            quantity = int(holding["quantity"])
            buy_locked = int(holding["buy_locked_quantity"])
            average_cost = float(holding["average_cost"])
            if (
                quantity < 0 or buy_locked < 0 or buy_locked > quantity
                or not math.isfinite(average_cost) or average_cost < 0
            ):
                raise ContractError("input holdings contain invalid quantities")
            if (
                previous_state_frame is not None
                and previous_state_manifest is not None
                and int(previous_state_manifest["row_count"]) > 0
            ):
                expected = previous_state_frame[
                    ["instrument", "quantity", "average_cost", "buy_locked_quantity"]
                ].sort_values("instrument").to_dict(orient="records")
                actual = [
                    {
                        "instrument": str(row["instrument"]),
                        "quantity": int(row["quantity"]),
                        "average_cost": float(row["average_cost"]),
                        "buy_locked_quantity": int(row["buy_locked_quantity"]),
                    }
                    for row in sorted(holdings.values(), key=lambda row: str(row["instrument"]))
                    if int(row["quantity"]) > 0
                ]
                if actual != expected:
                    raise ContractError("input holdings do not match previous state payload")
            if not same_session_prior_state:
                holding["buy_locked_quantity"] = 0
        for event in result_frame.to_dict(orient="records"):
            instrument = str(event["instrument"])
            filled = int(event["filled_quantity"])
            side = str(event["side"])
            if instrument not in holdings:
                if filled == 0:
                    continue
                if side != "buy":
                    raise ContractError(f"execution result references unknown holding {instrument}")
                holdings[instrument] = {
                    "instrument": instrument, "quantity": 0,
                    "average_cost": 0.0, "buy_locked_quantity": 0,
                }
            if filled == 0:
                continue
            if side == "buy":
                quantity = int(holdings[instrument]["quantity"])
                invested = quantity * float(holdings[instrument]["average_cost"])
                invested += filled * float(event["price"]) + float(event["fee_amount"])
                holdings[instrument]["quantity"] = quantity + filled
                holdings[instrument]["average_cost"] = invested / (quantity + filled)
                holdings[instrument]["buy_locked_quantity"] = (
                    int(holdings[instrument]["buy_locked_quantity"]) + filled
                )
            else:
                holdings[instrument]["quantity"] -= filled
                sellable_quantity = int(holdings[instrument]["quantity"]) + filled - int(
                    holdings[instrument]["buy_locked_quantity"]
                )
                if filled > sellable_quantity:
                    raise ContractError(f"execution sells T+1-locked inventory for {instrument}")
            if int(holdings[instrument]["quantity"]) < 0:
                raise ContractError(f"execution sells more than held quantity for {instrument}")
        return {key: value for key, value in holdings.items() if int(value["quantity"]) > 0}

    def _state_frame(
        self,
        cash: float,
        holdings: Mapping[str, Mapping[str, Any]],
        execution_market: Mapping[str, Mapping[str, Any]],
    ) -> pd.DataFrame:
        rows = []
        for instrument in sorted(holdings):
            holding = holdings[instrument]
            quantity = int(holding["quantity"])
            close = float((execution_market.get(instrument) or {}).get("close") or 0.0)
            if not math.isfinite(close) or close <= 0:
                raise ContractError(f"missing execution close for surviving holding {instrument}")
            buy_locked = int(holding["buy_locked_quantity"])
            if buy_locked < 0 or buy_locked > quantity:
                raise ContractError(f"invalid buy-locked quantity for {instrument}")
            rows.append({
                "instrument": instrument, "quantity": quantity,
                "average_cost": float(holding["average_cost"]),
                "buy_locked_quantity": buy_locked,
                "sellable_quantity": quantity - buy_locked,
                "market_value": quantity * close,
            })
        frame = pd.DataFrame(rows, columns=self.COLUMNS).astype(self.DTYPES)
        if frame["instrument"].duplicated().any():
            raise ContractError("paper state contains duplicate instruments")
        if not frame["sellable_quantity"].eq(frame["quantity"] - frame["buy_locked_quantity"]).all():
            raise ContractError("paper state sellable quantity does not reconcile")
        return frame

    def _manifest(
        self,
        config: Mapping[str, Any],
        frame: pd.DataFrame,
        *,
        result_manifest: Mapping[str, Any],
        previous_state_manifest: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        manifest = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "execution_id": config["execution_id"],
            "state_date": config["execution_date"],
            "state_mode": config["state_mode"],
            "cash": float(result_manifest["closing_cash"]),
            "data_file": "data.parquet",
            "data_checksum_sha256": "0" * 64,
            "columns": self.COLUMNS,
            "dtypes": self.DTYPES,
            "row_count": int(len(frame)),
            "key_uniqueness": "instrument",
            "logical_fingerprint": sha256_json(frame.to_dict(orient="records")),
            "serialization_profile_id": "parquet-v1",
            "target_weights_binding": self._binding(config["target_weights_binding"]),
            "previous_state_binding": (
                {
                    "family": "paper_portfolio_state_v1",
                    "generation_id": previous_state_manifest["generation_id"],
                    "manifest_digest_sha256": previous_state_manifest["manifest_digest_sha256"],
                }
                if previous_state_manifest
                else None
            ),
            "execution_result_binding": {
                "family": "execution_result_v1",
                "generation_id": result_manifest["generation_id"],
                "manifest_digest_sha256": result_manifest["manifest_digest_sha256"],
            },
            "risk_decision_binding": self._binding(config["risk_decision_binding"]),
            "market_data_binding": {
                "family": config["market_data_binding"]["dataset_family"],
                "generation_id": config["market_data_binding"]["generation_id"],
                "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
            },
            "run_id": config["run_id"],
            "created_at": config["created_at"],
            "quality_report_checksum_sha256": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
            "generation_id": "0" * 64,
        }
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name="paper_portfolio_state"
        )
        ModelContractLoader.validate("paper_portfolio_state", manifest)
        return manifest

    @staticmethod
    def _binding(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "family": value["family"],
            "generation_id": value["generation_id"],
            "manifest_digest_sha256": value["manifest_digest_sha256"],
        }
