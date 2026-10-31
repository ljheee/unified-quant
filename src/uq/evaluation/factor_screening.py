"""Read-only single-factor stability, collinearity, and turnover diagnostics."""
from __future__ import annotations

from itertools import combinations
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ..errors import ContractError
from ..contracts.gate_contracts import sha256_json
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


def select_factor_candidates(
    screening_report: dict[str, Any],
    *,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Create a validation-only shortlist from a factor screen report.

    This does not edit a reviewed factor set or authorize research-chain use.
    Candidates are filtered on explicitly configured validation metrics, then
    high-collinearity candidates are de-duplicated by a deterministic priority.
    Any pair with insufficient correlation history blocks both members.
    """
    required_policy = {
        "protocol_version", "score_directions", "minimum_valid_rank_ic_sessions",
        "minimum_rank_ic_t_statistic", "minimum_positive_rank_ic_fraction",
        "minimum_coverage_fraction", "minimum_pairwise_sessions",
        "maximum_absolute_daily_spearman", "maximum_mean_turnover_cost_bps",
        "maximum_selected_features",
    }
    if not isinstance(policy, dict) or set(policy) != required_policy:
        raise ContractError("factor selection policy fields do not match the v1 protocol")
    if policy["protocol_version"] != 1:
        raise ContractError("unsupported factor selection protocol version")
    integer_fields = (
        "minimum_valid_rank_ic_sessions", "minimum_pairwise_sessions",
        "maximum_selected_features",
    )
    if any(type(policy[name]) is not int or policy[name] < 1 for name in integer_fields):
        raise ContractError("factor selection count thresholds must be positive integers")
    for name in (
        "minimum_rank_ic_t_statistic", "minimum_positive_rank_ic_fraction",
        "minimum_coverage_fraction", "maximum_absolute_daily_spearman",
    ):
        value = policy[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not np.isfinite(float(value))):
            raise ContractError(f"factor selection threshold must be finite: {name}")
    if not 0 <= float(policy["minimum_positive_rank_ic_fraction"]) <= 1:
        raise ContractError("minimum positive RankIC fraction must be in [0, 1]")
    if float(policy["minimum_rank_ic_t_statistic"]) < 0:
        raise ContractError("minimum RankIC t-statistic threshold cannot be negative")
    if not 0 <= float(policy["minimum_coverage_fraction"]) <= 1:
        raise ContractError("minimum coverage fraction must be in [0, 1]")
    if not 0 < float(policy["maximum_absolute_daily_spearman"]) <= 1:
        raise ContractError("maximum absolute Spearman threshold must be in (0, 1]")
    maximum_cost = policy["maximum_mean_turnover_cost_bps"]
    if maximum_cost is not None and (
        isinstance(maximum_cost, bool) or not isinstance(maximum_cost, (int, float))
        or not np.isfinite(float(maximum_cost)) or maximum_cost < 0
    ):
        raise ContractError("maximum turnover cost must be null or finite and non-negative")
    directions = policy["score_directions"]
    if (not isinstance(directions, dict) or not directions
            or any(not isinstance(name, str) or not name.strip() for name in directions)
            or any(type(direction) is not int or direction not in {-1, 1}
                   for direction in directions.values())):
        raise ContractError("factor score directions must map each name to -1 or 1")
    if not isinstance(screening_report, dict):
        raise ContractError("factor screening report must be an object")
    window = screening_report.get("window")
    summaries = screening_report.get("feature_summary")
    pairwise = screening_report.get("pairwise_collinearity")
    if (not isinstance(window, dict) or type(window.get("session_count")) is not int
            or window["session_count"] < 1 or not isinstance(summaries, list)
            or not isinstance(pairwise, list)):
        raise ContractError("factor screening report lacks selection inputs")
    report_generation = screening_report.get("generation_id")
    if (not isinstance(report_generation, str) or len(report_generation) != 64
            or any(char not in "0123456789abcdef" for char in report_generation)):
        raise ContractError("factor selection requires a content-addressed screening report")
    if sha256_json({key: value for key, value in screening_report.items()
                    if key != "generation_id"}) != report_generation:
        raise ContractError("factor screening report content identity mismatch")
    summary_by_name = {item.get("feature"): item for item in summaries if isinstance(item, dict)}
    if set(summary_by_name) != set(directions) or len(summary_by_name) != len(summaries):
        raise ContractError("selection score directions must exactly cover screened features")

    def finite_metric(summary: dict[str, Any], key: str) -> float | None:
        value = summary.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(float(value)):
            raise ContractError(f"factor screen metric is invalid: {key}")
        return float(value)

    window_sessions = window["session_count"]
    decisions: dict[str, dict[str, Any]] = {}
    priority: dict[str, tuple[float, float, float, str]] = {}
    for name in sorted(directions):
        summary = summary_by_name[name]
        direction = directions[name]
        valid_sessions = summary.get("valid_rank_ic_sessions")
        if type(valid_sessions) is not int or valid_sessions < 0:
            raise ContractError(f"factor screen valid RankIC count is invalid: {name}")
        mean = finite_metric(summary, "mean_rank_ic")
        t_stat = finite_metric(summary, "rank_ic_t_statistic")
        positive_fraction = finite_metric(summary, "positive_rank_ic_fraction")
        coverage = finite_metric(summary, "feature_coverage_sessions")
        cost = finite_metric(summary, "mean_turnover_cost_proxy_bps_of_nav")
        if coverage is None:
            coverage_fraction = 0.0
        else:
            if type(summary.get("feature_coverage_sessions")) is not int or not 0 <= coverage <= window_sessions:
                raise ContractError(f"factor screen coverage count is invalid: {name}")
            coverage_fraction = coverage / window_sessions
        adjusted_mean = None if mean is None else mean * direction
        adjusted_t = None if t_stat is None else t_stat * direction
        adjusted_positive = (
            None if positive_fraction is None
            else positive_fraction if direction == 1 else 1.0 - positive_fraction
        )
        reasons: list[str] = []
        if valid_sessions < policy["minimum_valid_rank_ic_sessions"]:
            reasons.append("insufficient_rank_ic_sessions")
        if adjusted_mean is None or adjusted_mean <= 0:
            reasons.append("non_positive_direction_adjusted_mean_rank_ic")
        if adjusted_t is None or adjusted_t < float(policy["minimum_rank_ic_t_statistic"]):
            reasons.append("rank_ic_t_statistic_below_threshold")
        if (adjusted_positive is None
                or adjusted_positive < float(policy["minimum_positive_rank_ic_fraction"])):
            reasons.append("positive_rank_ic_fraction_below_threshold")
        if adjusted_positive is not None and not 0 <= adjusted_positive <= 1:
            raise ContractError(f"factor positive RankIC fraction is invalid: {name}")
        if coverage_fraction < float(policy["minimum_coverage_fraction"]):
            reasons.append("feature_coverage_below_threshold")
        if maximum_cost is not None and (
            cost is None or cost > float(maximum_cost)
        ):
            reasons.append("turnover_cost_proxy_missing_or_above_threshold")
        decisions[name] = {
            "score_direction": direction,
            "adjusted_mean_rank_ic": adjusted_mean,
            "adjusted_rank_ic_t_statistic": adjusted_t,
            "adjusted_positive_rank_ic_fraction": adjusted_positive,
            "feature_coverage_fraction": coverage_fraction,
            "mean_turnover_cost_proxy_bps_of_nav": cost,
            "status": "eligible" if not reasons else "rejected",
            "reasons": reasons,
        }
        priority[name] = (
            -(adjusted_t if adjusted_t is not None else float("-inf")),
            -(adjusted_mean if adjusted_mean is not None else float("-inf")),
            cost if cost is not None else float("inf"),
            name,
        )

    pair_map: dict[frozenset[str], dict[str, Any]] = {}
    for pair in pairwise:
        if not isinstance(pair, dict):
            raise ContractError("factor screen collinearity entry is invalid")
        left, right = pair.get("feature_left"), pair.get("feature_right")
        if left not in directions or right not in directions or left == right:
            raise ContractError("factor screen collinearity pair does not match selected features")
        pair_key = frozenset((left, right))
        if pair_key in pair_map:
            raise ContractError("factor screen contains duplicate collinearity pairs")
        pair_map[pair_key] = pair
    expected_pairs = len(directions) * (len(directions) - 1) // 2
    if len(pair_map) != expected_pairs:
        raise ContractError("factor screen does not cover every candidate collinearity pair")
    for (left, right), pair in (
        ((a, b), pair_map[frozenset((a, b))])
        for index, a in enumerate(sorted(directions))
        for b in sorted(directions)[index + 1:]
    ):
        valid = pair.get("valid_sessions")
        correlation = finite_metric(pair, "mean_absolute_daily_spearman")
        if type(valid) is not int or valid < 0 or valid > window_sessions:
            raise ContractError("factor screen pairwise valid-session count is invalid")
        if correlation is not None and not 0 <= correlation <= 1:
            raise ContractError("factor screen absolute Spearman metric is outside [0, 1]")
        if type(valid) is not int or valid < policy["minimum_pairwise_sessions"] or correlation is None:
            for name in (left, right):
                if "pairwise_collinearity_unmeasured" not in decisions[name]["reasons"]:
                    decisions[name]["reasons"].append("pairwise_collinearity_unmeasured")
                    decisions[name]["status"] = "rejected"

    ranked_eligible = sorted(
        (name for name, decision in decisions.items() if decision["status"] == "eligible"),
        key=lambda name: priority[name],
    )
    selected: list[str] = []
    for name in ranked_eligible:
        conflict = None
        for chosen in selected:
            pair = pair_map[frozenset((name, chosen))]
            correlation = pair["mean_absolute_daily_spearman"]
            if correlation >= float(policy["maximum_absolute_daily_spearman"]):
                conflict = chosen
                break
        if conflict is not None:
            decisions[name]["status"] = "deprioritized"
            decisions[name]["reasons"].append(f"collinear_with_higher_priority:{conflict}")
        elif len(selected) >= policy["maximum_selected_features"]:
            decisions[name]["status"] = "deprioritized"
            decisions[name]["reasons"].append("maximum_selected_features_reached")
        else:
            selected.append(name)

    document = {
        "record_version": 1,
        "kind": "factor_selection_shortlist",
        "publication_scope": "validation_only_shortlist_no_factor_promotion",
        "screen_report_generation_id": report_generation,
        "validation_window": dict(window),
        "policy": dict(policy),
        "selected_candidates": selected,
        "decisions": [decisions[name] | {"feature": name} for name in sorted(decisions)],
        "limitations": [
            "validation-only selection; do not use this window as final out-of-sample evidence",
            "no multiple-testing correction across research programs",
            "shortlist does not alter reviewed factor definitions or authorize production use",
        ],
    }
    document["generation_id"] = sha256_json(document)
    return document


def _optional_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not np.isfinite(number):
        raise ContractError("factor screen produced a non-finite metric")
    return number
