"""Validation-only LightGBM candidate selection over a governed dataset frame.

This research entry point is deliberately separate from the accepted model
artifact contracts. It consumes a dataset manifest/frame already verified by
DatasetWriter.read, records the dataset identity in its receipt, and never
lets test-period labels influence model selection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..contracts.model_layer import ModelContractLoader
from ..errors import ContractError
from .dataset import SplitValidator


DEFAULT_CANDIDATES: tuple[dict[str, Any], ...] = (
    {"n_estimators": 80, "num_leaves": 15, "learning_rate": 0.05, "min_child_samples": 40},
    {"n_estimators": 120, "num_leaves": 31, "learning_rate": 0.03, "min_child_samples": 60},
    {"n_estimators": 60, "num_leaves": 7, "learning_rate": 0.08, "min_child_samples": 30},
)


@dataclass(frozen=True)
class LightGBMSelectionResult:
    receipt: dict[str, Any]
    model_text: bytes
    test_predictions: pd.DataFrame


def _split(manifest: Mapping[str, Any], name: str) -> tuple[str, str]:
    matches = [item for item in manifest["split_policy"]["splits"] if item["name"] == name]
    if len(matches) != 1:
        raise ContractError(f"LightGBM selection requires exactly one {name} split")
    return matches[0]["start_date"], matches[0]["end_date"]


def _rank_ic_by_date(predictions: pd.DataFrame, label_column: str) -> tuple[float, int]:
    values: list[float] = []
    for _, day in predictions.groupby("datetime", sort=True):
        valid = day["score"].notna() & day[label_column].notna()
        subset = day.loc[valid]
        if len(subset) < 2 or subset["score"].nunique() < 2 or subset[label_column].nunique() < 2:
            continue
        value = subset["score"].rank(method="average").corr(
            subset[label_column].rank(method="average"), method="pearson"
        )
        if pd.notna(value):
            values.append(float(value))
    if not values:
        raise ContractError("validation split has no usable cross-sectional RankIC")
    return float(np.mean(values)), len(values)


def select_lightgbm_candidate(
    *,
    dataset_manifest: dict[str, Any],
    dataset_frame: pd.DataFrame,
    feature_columns: Sequence[str] | None = None,
    label_column: str = "label",
    candidates: Sequence[Mapping[str, Any]] = DEFAULT_CANDIDATES,
    seed: int = 7,
    thread_count: int = 1,
) -> LightGBMSelectionResult:
    """Select one bounded candidate using validation RankIC, then score test.

    The final model is refit on train only after the single selection decision,
    matching the existing qlib_linear fit window. Test labels are never passed
    to fit or the selection metric.
    """
    ModelContractLoader.validate("model_dataset", dataset_manifest)
    if not candidates or len(candidates) > 3:
        raise ContractError("LightGBM trial budget is fixed at one to three candidates")
    if (type(seed) is not int or seed < 0
            or type(thread_count) is not int or thread_count < 1):
        raise ContractError("LightGBM seed must be non-negative and thread_count positive")
    features = list(feature_columns or dataset_manifest["ordered_features"])
    if features != dataset_manifest["ordered_features"]:
        raise ContractError("LightGBM feature columns must match the governed dataset order")
    if not features or len(features) != len(set(features)) or label_column in features:
        raise ContractError("LightGBM requires unique feature columns separate from the label")
    required = {"instrument", "datetime", label_column, *features}
    if not required.issubset(dataset_frame.columns):
        raise ContractError("LightGBM dataset frame is missing required columns")

    train_start, train_end = _split(dataset_manifest, "train")
    validation_start, validation_end = _split(dataset_manifest, "validation")
    test_start, test_end = _split(dataset_manifest, "test")
    dates = pd.to_datetime(dataset_frame["datetime"]).dt.strftime("%Y-%m-%d")
    feature_values = dataset_frame[features].to_numpy(dtype="float64", na_value=np.nan)
    if np.isinf(feature_values).any():
        raise ContractError("LightGBM dataset features contain infinite values")
    frame = dataset_frame.copy()
    frame["_day"] = dates
    SplitValidator.validate_splits(
        dataset_manifest["split_policy"]["splits"],
        horizon=dataset_manifest["split_policy"]["purge_trading_days"],
        embargo_days=dataset_manifest["split_policy"]["embargo_trading_days"],
        trading_dates=sorted(frame["_day"].unique().tolist()),
        covered_dates=set(frame["_day"]),
    )
    train_mask = frame["_day"].between(train_start, train_end)
    validation_mask = frame["_day"].between(validation_start, validation_end)
    test_mask = frame["_day"].between(test_start, test_end)
    # Null labels are allowed outside the supervised rows; candidate train and
    # validation rows must be complete, matching the published sample policy.
    train = frame.loc[train_mask & frame[label_column].notna()].copy()
    validation = frame.loc[validation_mask & frame[label_column].notna()].copy()
    test = frame.loc[test_mask, ["instrument", "datetime", *features]].copy()
    if train.empty or validation.empty or test.empty:
        raise ContractError("LightGBM requires non-empty train, validation, and test splits")
    if not np.isfinite(train[label_column].to_numpy(dtype="float64")).all():
        raise ContractError("LightGBM train labels contain non-finite values")
    if not np.isfinite(validation[label_column].to_numpy(dtype="float64")).all():
        raise ContractError("LightGBM validation labels contain non-finite values")
    for part_name, part in (("train", train), ("validation", validation), ("test", test)):
        values = part[features].to_numpy(dtype="float64", na_value=np.nan)
        if not np.isfinite(values).all():
            raise ContractError(f"LightGBM {part_name} features contain null or non-finite values")

    try:
        import lightgbm
        from lightgbm import LGBMRegressor
    except ImportError as exc:
        raise ContractError("LightGBM candidate selection requires the gbdt extra") from exc

    trial_receipts = []
    for index, params in enumerate(candidates):
        model = LGBMRegressor(
            objective="regression",
            random_state=seed,
            n_jobs=thread_count,
            deterministic=True,
            force_col_wise=True,
            verbosity=-1,
            **dict(params),
        )
        model.fit(train[features], train[label_column].astype("float64"))
        scored = validation[["instrument", "datetime", label_column]].copy()
        scored["score"] = model.predict(validation[features])
        rank_ic, date_count = _rank_ic_by_date(scored, label_column)
        trial_receipts.append({
            "trial_index": index,
            "parameters": dict(params),
            "validation_mean_daily_rank_ic": rank_ic,
            "validation_cross_section_count": date_count,
        })

    selected_index = max(
        range(len(trial_receipts)),
        key=lambda index: (trial_receipts[index]["validation_mean_daily_rank_ic"], -index),
    )
    selected = dict(candidates[selected_index])
    refit = LGBMRegressor(
        objective="regression",
        random_state=seed,
        n_jobs=thread_count,
        deterministic=True,
        force_col_wise=True,
        verbosity=-1,
        **selected,
    )
    refit.fit(train[features], train[label_column].astype("float64"))
    model_text = refit.booster_.model_to_string().encode("utf-8")
    prediction = test[["instrument", "datetime"]].copy()
    prediction["score"] = refit.predict(test[features])
    prediction = prediction.sort_values(["datetime", "instrument"], kind="mergesort").reset_index(drop=True)
    receipt = {
        "protocol": "lightgbm-validation-selection-v1",
        "lightgbm_version": lightgbm.__version__,
        "dataset_generation_id": dataset_manifest["generation_id"],
        "dataset_manifest_digest_sha256": dataset_manifest["manifest_digest_sha256"],
        "dataset_data_checksum_sha256": dataset_manifest["data_checksum_sha256"],
        "feature_columns": features,
        "label_column": label_column,
        "train_period": [train_start, train_end],
        "validation_period": [validation_start, validation_end],
        "test_period": [test_start, test_end],
        "selection_metric": "mean_daily_cross_sectional_rank_ic",
        "test_labels_used_for_selection": False,
        "trial_budget": len(candidates),
        "trials": trial_receipts,
        "selected_trial_index": selected_index,
        "selected_parameters": selected,
        "seed": seed,
        "thread_count": thread_count,
        "model_sha256": hashlib.sha256(model_text).hexdigest(),
        "test_prediction_rows": len(prediction),
        "test_prediction_sha256": hashlib.sha256(
            prediction.to_csv(index=False, float_format="%.17g").encode("utf-8")
        ).hexdigest(),
    }
    return LightGBMSelectionResult(receipt, model_text, prediction)


def receipt_json(receipt: Mapping[str, Any]) -> str:
    """Serialize a stable receipt for local experiment evidence."""
    return json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n"
