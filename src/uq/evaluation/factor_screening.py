"""Read-only single-factor stability, collinearity, and turnover diagnostics."""
from __future__ import annotations

from itertools import combinations
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ..errors import ContractError
from .metrics import information_coefficients, session_dates


def screen_factor_panel(
    panel: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    features: Sequence[str],
    calendar_dates: Sequence[str],
    horizon_sessions: int,
    top_n: int = 30,
    correlation_threshold: float = 0.90,
    hac_lags: int | None = None,
    cost_proxy_bps_per_one_way_turnover: float | None = None,
) -> dict[str, Any]:
    """Screen each feature without selecting or dropping factors automatically.

    Rank IC is calculated once per calendar session and its mean uses the same
    overlap-aware HAC routine as prediction evaluation. The turnover statistic
    is the one-way L1 change of equal-weight top-N membership; it is a ranking
    churn proxy and does not include orders, costs, or trading constraints.
    Pairwise collinearity is the mean absolute daily Spearman correlation over
    pairwise-complete cross-sections, so opposite-signed correlation does not
    disappear through time averaging.
    """
    dates = session_dates(calendar_dates, label="factor screen calendar")
    if not dates or dates != sorted(set(dates)):
        raise ContractError("factor screen calendar must be nonempty, sorted and unique")
    if type(horizon_sessions) is not int or horizon_sessions < 1:
        raise ContractError("factor screen label horizon must be a positive integer")
    lags = horizon_sessions - 1 if hac_lags is None else hac_lags
    if type(lags) is not int or lags < horizon_sessions - 1:
        raise ContractError("factor screen HAC lags must cover the overlapping label horizon")
    if type(top_n) is not int or top_n < 1:
        raise ContractError("factor screen top_n must be a positive integer")
    if (not isinstance(correlation_threshold, (int, float))
            or isinstance(correlation_threshold, bool)
            or not np.isfinite(float(correlation_threshold))
            or not 0 < float(correlation_threshold) <= 1):
        raise ContractError("factor correlation threshold must be finite and in (0, 1]")
    if cost_proxy_bps_per_one_way_turnover is not None and (
        isinstance(cost_proxy_bps_per_one_way_turnover, bool)
        or not isinstance(cost_proxy_bps_per_one_way_turnover, (int, float))
        or not np.isfinite(float(cost_proxy_bps_per_one_way_turnover))
        or cost_proxy_bps_per_one_way_turnover < 0
    ):
        raise ContractError("factor turnover cost proxy must be finite and non-negative")
    feature_names = list(features)
    if (not feature_names or any(not isinstance(name, str) or not name.strip() for name in feature_names)
            or len(feature_names) != len(set(feature_names))):
        raise ContractError("factor screen features must be nonempty unique names")
    reserved = {"instrument", "decision_date", "label"}
    if reserved.intersection(feature_names):
        raise ContractError("factor screen feature name conflicts with a reserved column")
    required_panel = {"instrument", "decision_date", *feature_names}
    if panel.empty or required_panel - set(panel):
        raise ContractError(f"factor screen panel is empty or missing columns: {sorted(required_panel - set(panel))}")
    if {"instrument", "decision_date", "label"} - set(labels):
        raise ContractError("factor screen labels require instrument, decision_date and label")

    work = panel[["instrument", "decision_date", *feature_names]].copy()
    work["decision_date"] = session_dates(work["decision_date"].tolist(), label="factor panel")
    if not set(work["decision_date"]) <= set(dates):
        raise ContractError("factor panel contains dates outside the declared calendar")
    if work["instrument"].isna().any() or not work["instrument"].map(
        lambda item: isinstance(item, str) and bool(item.strip())
    ).all():
        raise ContractError("factor panel contains invalid instruments")
    if work.duplicated(["instrument", "decision_date"]).any():
        raise ContractError("factor panel contains duplicate instrument/date keys")
    for name in feature_names:
        values = work[name]
        if not pd.api.types.is_numeric_dtype(values) or pd.api.types.is_bool_dtype(values):
            raise ContractError(f"factor feature must be numeric: {name}")
        non_null = values.dropna().to_numpy(dtype=float)
        if not np.isfinite(non_null).all():
            raise ContractError(f"factor feature contains non-finite values: {name}")

    label_frame = labels[["instrument", "decision_date", "label"]].copy()
    label_frame["decision_date"] = session_dates(
        label_frame["decision_date"].tolist(), label="factor labels"
    )
    if label_frame["instrument"].isna().any() or not label_frame["instrument"].map(
        lambda item: isinstance(item, str) and bool(item.strip())
    ).all():
        raise ContractError("factor labels contain invalid instruments")
    if label_frame.duplicated(["instrument", "decision_date"]).any():
        raise ContractError("factor labels contain duplicate instrument/date keys")
    label_frame = label_frame.loc[label_frame["decision_date"].isin(dates)].reset_index(drop=True)

    daily_rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for name in feature_names:
        values = work[["instrument", "decision_date", name]].rename(columns={name: "score"})
        eligible = values.loc[values["score"].notna()].copy()
        ic_summary, ic_daily = information_coefficients(
            eligible,
            label_frame,
            calendar_dates=dates,
            horizon_sessions=horizon_sessions,
            hac_lags=lags,
        )
        feature_daily: list[dict[str, Any]] = []
        previous_selection: set[str] | None = None
        previous_date_index: int | None = None
        for date_index, day in enumerate(dates):
            cross_section = eligible.loc[eligible["decision_date"] == day]
            ordered = cross_section.sort_values(
                ["score", "instrument"], ascending=[False, True], kind="mergesort"
            )
            selected = set(ordered.head(top_n)["instrument"].tolist())
            turnover = None
            turnover_status = "insufficient_eligible_names" if len(ordered) < top_n else "first_session"
            if len(ordered) >= top_n:
                if previous_selection is not None and previous_date_index == date_index - 1:
                    keys = previous_selection | selected
                    turnover = 0.5 * sum(
                        abs((1 / top_n if key in selected else 0.0)
                            - (1 / top_n if key in previous_selection else 0.0))
                        for key in keys
                    )
                    turnover_status = "estimated"
                previous_selection = selected
                previous_date_index = date_index
            else:
                previous_selection = None
                previous_date_index = None
            ic_row = ic_daily.iloc[date_index]
            cost_proxy = (
                turnover * float(cost_proxy_bps_per_one_way_turnover)
                if turnover is not None and cost_proxy_bps_per_one_way_turnover is not None
                else None
            )
            record = {
                "decision_date": day,
                "feature": name,
                "eligible_instruments": int(len(ordered)),
                "ic_pairs": int(ic_row["pairs"]),
                "ic": _optional_float(ic_row["ic"]),
                "rank_ic": _optional_float(ic_row["rank_ic"]),
                "ic_status": str(ic_row["status"]),
                "selected_count": len(selected),
                "top_n_one_way_turnover_proxy": turnover,
                "turnover_cost_proxy_bps_of_nav": cost_proxy,
                "turnover_status": turnover_status,
            }
            daily_rows.append(record)
            feature_daily.append(record)

        valid_rank_ic = [row["rank_ic"] for row in feature_daily if row["rank_ic"] is not None]
        turnovers = [row["top_n_one_way_turnover_proxy"] for row in feature_daily
                     if row["top_n_one_way_turnover_proxy"] is not None]
        turnover_costs = [row["turnover_cost_proxy_bps_of_nav"] for row in feature_daily
                          if row["turnover_cost_proxy_bps_of_nav"] is not None]
        rank_summary = ic_summary["rank_ic"]
        summaries.append({
            "feature": name,
            "valid_ic_sessions": int(ic_summary["ic"]["valid_sessions"]),
            "valid_rank_ic_sessions": int(rank_summary["valid_sessions"]),
            "mean_ic": ic_summary["ic"]["mean"],
            "mean_rank_ic": rank_summary["mean"],
            "rank_ic_standard_error": rank_summary["standard_error"],
            "rank_ic_t_statistic": rank_summary["t_statistic"],
            "rank_ic_effective_sessions": rank_summary["effective_sessions"],
            "positive_rank_ic_fraction": (
                float(np.mean(np.asarray(valid_rank_ic) > 0)) if valid_rank_ic else None
            ),
            "feature_coverage_sessions": sum(row["eligible_instruments"] > 0 for row in feature_daily),
            "top_n_eligible_sessions": sum(row["selected_count"] == top_n for row in feature_daily),
            "mean_top_n_one_way_turnover_proxy": float(np.mean(turnovers)) if turnovers else None,
            "mean_turnover_cost_proxy_bps_of_nav": (
                float(np.mean(turnover_costs)) if turnover_costs else None
            ),
            "turnover_transitions": len(turnovers),
        })
    pairwise: list[dict[str, Any]] = []
    for left, right in combinations(feature_names, 2):
        correlations: list[float] = []
        for day in dates:
            cross_section = work.loc[work["decision_date"] == day, [left, right]].dropna()
            if len(cross_section) < 3 or cross_section[left].nunique() < 2 or cross_section[right].nunique() < 2:
                continue
            ranks = cross_section.rank(method="average")
            correlation = float(ranks[left].corr(ranks[right]))
            if np.isfinite(correlation):
                correlations.append(correlation)
        mean_signed = float(np.mean(correlations)) if correlations else None
        mean_absolute = float(np.mean(np.abs(correlations))) if correlations else None
        pairwise.append({
            "feature_left": left,
            "feature_right": right,
            "valid_sessions": len(correlations),
            "mean_daily_spearman": mean_signed,
            "mean_absolute_daily_spearman": mean_absolute,
            "high_collinearity": bool(
                mean_absolute is not None and mean_absolute >= float(correlation_threshold)
            ),
        })

    return {
        "protocol": {
            "label_horizon_sessions": horizon_sessions,
            "hac_lags": lags,
            "top_n": top_n,
            "top_n_turnover_definition": "equal_weight_membership_half_l1_change; diagnostic_proxy_only",
            "turnover_cost_proxy_definition": (
                "not_computed_without_reviewed_v3_cost_config"
                if cost_proxy_bps_per_one_way_turnover is None
                else "fees_slippage_and_impact_at_participation_cap; ignores_minimum_fees_fills_and_constraints"
            ),
            "collinearity_definition": "mean_absolute_daily_spearman_pairwise_complete",
            "correlation_threshold": float(correlation_threshold),
            "factor_score_direction": "raw_values_descending_for_top_n_turnover_proxy",
            "missing_feature_policy": "drop_null_per_feature_and_preserve_calendar_positions",
        },
        "summary": summaries,
        "pairwise_collinearity": pairwise,
        "daily": daily_rows,
    }


def _optional_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not np.isfinite(number):
        raise ContractError("factor screen produced a non-finite metric")
    return number
