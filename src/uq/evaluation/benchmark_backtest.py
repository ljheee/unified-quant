"""Publish a PIT benchmark replay through the reviewed v3/v4 engine."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest.engine import BacktestEngine, BacktestResultStore
from ..backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding
from ..contracts.artifacts import UniverseSnapshotStore
from ..contracts.index_membership import IndexMembershipStore
from ..errors import ContractError
from ..research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore, FactorExposureStore
from .benchmark import BenchmarkScheme, BenchmarkWeightStore, build_benchmark_weight_partitions, read_pit_benchmark_membership
from .report import _verified_result


def _terminal_liquidation_mode(
    walk_forward_root: Path | str | None,
    strategy: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    if walk_forward_root is None:
        return False
    from .walk_forward_continuous import WalkForwardContinuousStore

    receipt = WalkForwardContinuousStore(walk_forward_root).read(strategy["generation_id"])
    if (
        receipt.get("backtest_result_generation_id") != strategy.get("generation_id")
        or receipt.get("backtest_result_manifest_digest_sha256")
        != strategy.get("manifest_digest_sha256")
        or receipt.get("backtest_config_generation_id") != config.get("generation_id")
        or receipt.get("terminal_liquidation_attempted") is not True
    ):
        raise ContractError("walk-forward receipt does not authorize matching terminal-liquidation replay")
    return True


def publish_benchmark_backtest(
    *,
    data_root: Path | str,
    benchmark_root: Path | str,
    membership_root: Path | str,
    strategy_result_generation_id: str,
    scheme: BenchmarkScheme,
    quality_decision: Any,
    exposure_root: Path | str | None = None,
    exposure_generation_id: str | None = None,
    walk_forward_root: Path | str | None = None,
) -> dict[str, Any]:
    """Build, immutably publish and execute a benchmark on a strategy's schedule.

    The strategy result supplies the reviewed v3/v4 config and rebalance dates. The
    benchmark membership must be the same named universe and every benchmark
    constituent must also belong to the strategy's configured universe. All
    execution inputs are re-read through their owning stores before the common
    engine and result publisher run.
    """
    root = Path(data_root)
    strategy, config, _ = _verified_result(root, strategy_result_generation_id)
    if (strategy.get("contract_version") not in {3, 4}
            or config.get("contract_version") != strategy.get("contract_version")):
        raise ContractError("benchmark publication requires a reviewed v3/v4 strategy result and config")
    if strategy["backtest_config_generation_id"] != config["generation_id"]:
        raise ContractError("strategy result config binding is inconsistent")
    terminal_liquidation = _terminal_liquidation_mode(
        walk_forward_root, strategy, config
    )

    config_binding = {
        "generation_id": config["generation_id"],
        "manifest_digest_sha256": config["manifest_digest_sha256"],
    }
    schedule_dates = [item["decision_date"] for item in strategy["target_weight_bindings"]]
    if schedule_dates != sorted(set(schedule_dates)) or not schedule_dates:
        raise ContractError("strategy target-weight schedule must be sorted and nonempty")

    market_store = BacktestMarketDatasetStore(root)
    calendar_frame = market_store.read_frame(
        config["calendar_binding"]["generation_id"],
        config["calendar_binding"]["checksum_sha256"],
        dataset_type="calendar",
        required_columns={"date"},
    )
    calendar_dates = sorted(calendar_frame["date"].astype(str).unique().tolist())
    start_date, end_date = config["start_date"], config["end_date"]
    if not calendar_dates or calendar_dates[0] != start_date or calendar_dates[-1] != end_date:
        raise ContractError("benchmark calendar does not exactly cover the reviewed config window")
    if not set(schedule_dates) <= set(calendar_dates):
        raise ContractError("strategy rebalance dates fall outside the governed calendar")

    universe_store = UniverseSnapshotStore(root)
    universe_generation = config["universe_binding"]["snapshot_generation_id"]
    universe_manifest = universe_store.read_manifest(universe_generation)
    universe_id = universe_manifest["universe_id"]
    strategy_universe = universe_store.read_members(
        universe_generation,
        universe_id=universe_id,
        requested_valid_from=date.fromisoformat(start_date),
        requested_valid_to=date.fromisoformat(end_date),
    )
    allowed_instruments = set(strategy_universe["instrument"].astype(str))
    strategy_universe_binding = {
        "generation_id": universe_generation,
        "universe_id": universe_id,
        "members_checksum_sha256": universe_manifest["members_artifact"]["checksum_sha256"],
    }

    suspension = market_store.read_frame(
        config["suspension_binding"]["dataset_generation_id"],
        config["suspension_binding"]["data_checksum_sha256"],
        dataset_type="suspension",
        required_columns={"date", "instrument", "suspended"},
    )
    corporate_action_events = None
    if config["contract_version"] == 4:
        from ..backtest.corporate_actions import ACTION_EVENT_COLUMNS, normalize_action_events

        corporate = market_store.read_frame(
            config["corporate_action_binding"]["dataset_generation_id"],
            config["corporate_action_binding"]["data_checksum_sha256"],
            dataset_type="corporate_action_events", required_columns=set(ACTION_EVENT_COLUMNS),
        )
        schedule = normalize_action_events(
            corporate, start_date=start_date, end_date=end_date, trading_dates=calendar_dates,
        )
        corporate_action_instruments = schedule["rights_issue_instruments"]
        corporate_action_events = corporate
    else:
        corporate = market_store.read_frame(
            config["corporate_action_binding"]["dataset_generation_id"],
            config["corporate_action_binding"]["data_checksum_sha256"],
            dataset_type="corporate_action", required_columns={"date", "instrument"},
        )
        corporate_action_instruments = set(corporate["instrument"].astype(str))
    allowed_instruments -= corporate_action_instruments
    if not allowed_instruments:
        raise ContractError("benchmark strategy universe is empty after corporate-action exclusions")

    membership, membership_set = read_pit_benchmark_membership(
        IndexMembershipStore(membership_root),
        universe_id=universe_id,
        calendar_dates=calendar_dates,
        start_date=start_date,
        end_date=end_date,
    )
    membership = membership.loc[
        membership["instrument"].isin(allowed_instruments)
        & membership["date"].isin(schedule_dates),
        ["date", "instrument"],
    ].reset_index(drop=True)
    if membership.empty:
        raise ContractError("benchmark membership has no strategy-universe constituents on its schedule")

    exposure_binding = None
    exposure_panel = None
    required_control = {
        "market_cap_weighted": "market_cap",
        "industry_neutral": "industry",
    }.get(scheme)
    if required_control is not None:
        if exposure_root is None or exposure_generation_id is None:
            raise ContractError(f"{scheme} benchmark requires a published PIT exposure panel")
        exposure_manifest, exposure_panel = FactorExposureStore(exposure_root).read_frame(
            exposure_generation_id
        )
        if required_control not in exposure_manifest["controls"]:
            raise ContractError(f"PIT exposure panel does not contain {required_control}")
        exposure_panel = exposure_panel.copy()
        exposure_panel["date"] = pd.to_datetime(exposure_panel["datetime"]).dt.strftime("%Y-%m-%d")
        exposure_panel = exposure_panel.loc[
            exposure_panel["date"].isin(schedule_dates)
            & exposure_panel["instrument"].isin(allowed_instruments)
        ]
        exposure_binding = {
            "generation_id": exposure_manifest["generation_id"],
            "manifest_digest_sha256": exposure_manifest["manifest_digest_sha256"],
            "data_checksum_sha256": exposure_manifest["data_checksum_sha256"],
            "controls": exposure_manifest["controls"],
            "source_id": exposure_manifest["source_id"],
            "source_approval_ref": exposure_manifest["source_approval_ref"],
        }

    if scheme == "market_cap_weighted":
        if exposure_panel is None:
            raise ContractError("market-cap benchmark exposure panel is unavailable")
        weights = build_benchmark_weight_partitions(
            membership,
            scheme=scheme,
            market_cap=exposure_panel[["date", "instrument", "market_cap"]],
        )
    elif scheme == "industry_neutral":
        if exposure_panel is None:
            raise ContractError("industry benchmark exposure panel is unavailable")
        weights = build_benchmark_weight_partitions(
            membership,
            scheme=scheme,
            industry=exposure_panel[["date", "instrument", "industry"]],
        )
    else:
        weights = build_benchmark_weight_partitions(membership, scheme=scheme)
    if sorted(weights) != schedule_dates:
        raise ContractError("PIT membership does not cover every strategy rebalance date")

    schedule_binding = {
        "generation_id": strategy["generation_id"],
        "manifest_digest_sha256": strategy["manifest_digest_sha256"],
        "decision_dates": schedule_dates,
    }
    weight_store = BenchmarkWeightStore(benchmark_root)
    panel_manifest = weight_store.publish(
        weights,
        scheme=scheme,
        universe_id=universe_id,
        membership_set=membership_set,
        membership_panel=membership,
        backtest_config_binding=config_binding,
        strategy_schedule_binding=schedule_binding,
        strategy_universe_binding=strategy_universe_binding,
        terminal_liquidation=terminal_liquidation,
        exposure_binding=exposure_binding,
    )
    panel_manifest, weight_partitions = weight_store.read(
        panel_manifest["generation_id"], membership_panel=membership
    )

    price_binding, raw = RawExecutionPriceStore(root).read_window(
        start_date, end_date, config["price_source_binding"]
    )
    if price_binding != config["price_source_binding"]:
        raise ContractError("raw price partition set does not match the reviewed config")
    raw["date"] = pd.to_datetime(raw["session_date"]).dt.strftime("%Y-%m-%d")
    price_panel = raw.set_index(["date", "instrument"])[["open", "close", "volume"]]
    price_dates = sorted(set(price_panel.index.get_level_values("date")))
    if price_dates != calendar_dates:
        raise ContractError("benchmark raw prices do not match the governed calendar")

    fact_manifest, execution_facts = ExecutionFactsStore(root).read(
        config["execution_facts_binding"]["generation_id"]
    )
    if facts_binding(fact_manifest) != config["execution_facts_binding"]:
        raise ContractError("benchmark execution facts binding mismatch")
    suspension_dates = {
        (str(row.date), str(row.instrument))
        for row in suspension.loc[suspension["suspended"].astype(bool)].itertuples(index=False)
    }

    result_manifest, result_frames = BacktestEngine(root).run(
        config=config,
        portfolio_definition={"generation_id": panel_manifest["generation_id"]},
        weight_partitions=weight_partitions,
        price_panel=price_panel,
        execution_facts=execution_facts,
        suspension_dates=suspension_dates,
        corporate_action_instruments=corporate_action_instruments,
        corporate_action_events=corporate_action_events,
        terminal_liquidation=terminal_liquidation,
    )
    target_bindings = [
        {
            "decision_date": item["decision_date"],
            "generation_id": item["generation_id"],
            "manifest_digest_sha256": item["manifest_digest_sha256"],
        }
        for item in panel_manifest["partitions"]
    ]
    result_store = BacktestResultStore(root)
    try:
        result_store.publish(
            result_manifest,
            result_frames,
            quality_decision=quality_decision,
            target_weight_generations=target_bindings,
        )
    except ContractError as exc:
        # A completed immutable result may be safely retried only after the
        # owning reader verifies the existing generation and exact artifacts.
        generation = result_manifest.get("generation_id")
        if "already exists" not in str(exc) or not isinstance(generation, str):
            raise
        existing_manifest, existing_frames = result_store.read(generation)
        if existing_manifest != result_manifest:
            raise ContractError("existing benchmark result differs from retry") from exc
        try:
            for name in ("equity_curve", "daily_metrics", "fills"):
                pd.testing.assert_frame_equal(
                    existing_frames[name].reset_index(drop=True),
                    result_frames[name].reset_index(drop=True),
                    check_dtype=False,
                    check_exact=True,
                )
        except AssertionError as mismatch:
            raise ContractError("existing benchmark result artifacts differ from retry") from mismatch
    result_manifest, _ = result_store.read(result_manifest["generation_id"])
    return {
        "record_version": 1,
        "kind": "benchmark_backtest_publication",
        "scheme": scheme,
        "benchmark_weight_generation_id": panel_manifest["generation_id"],
        "benchmark_weight_manifest_digest_sha256": panel_manifest["manifest_digest_sha256"],
        "strategy_result_generation_id": strategy["generation_id"],
        "backtest_config_generation_id": config["generation_id"],
        "backtest_result_generation_id": result_manifest["generation_id"],
        "backtest_result_manifest_digest_sha256": result_manifest["manifest_digest_sha256"],
        "window": {"start_date": start_date, "end_date": end_date},
        "decision_dates": schedule_dates,
        "terminal_liquidation_attempted": terminal_liquidation,
        "terminal_liquidation_completed": (
            float(result_frames["equity_curve"]["holdings_value"].iloc[-1]) <= 1e-8
            if terminal_liquidation and not result_frames["equity_curve"].empty
            else None
        ),
        "membership_precision": "monthly_approximation",
        "signed_by_publisher": False,
    }
