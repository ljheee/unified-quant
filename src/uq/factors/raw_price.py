from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from ..errors import ContractError


_REQUIRED_COLUMNS = ["instrument", "datetime", "high", "low", "close", "volume", "amount"]


def calculate_raw_price_factors(frame: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(set(_REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise ContractError(f"missing raw-price input columns: {missing}")
    if frame[["instrument", "datetime"]].isna().any().any():
        raise ContractError("raw-price factor keys cannot be null")
    if frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("duplicate canonical keys reject factor calculation")
    expected = set(map(tuple, frame[["instrument", "datetime"]].to_numpy()))
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)

    output = ordered[["instrument", "datetime"]].copy()
    high = pd.to_numeric(ordered["high"], errors="raise")
    low = pd.to_numeric(ordered["low"], errors="raise")
    close = pd.to_numeric(ordered["close"], errors="raise")
    volume = pd.to_numeric(ordered["volume"], errors="raise")
    amount = pd.to_numeric(ordered["amount"], errors="raise")

    output["range_ratio_1d"] = (high - low) / close.replace(0, np.nan)
    denominator = high - low
    output["close_location_1d"] = np.where(denominator == 0, np.nan, (close - low) / denominator.where(denominator != 0))
    grouped_amount = amount.groupby(ordered["instrument"], sort=False)
    output["amount_20d"] = grouped_amount.transform(lambda values: values.rolling(20, min_periods=20).mean())
    rolling_volume_mean = volume.groupby(ordered["instrument"], sort=False).transform(lambda values: values.rolling(20, min_periods=20).mean())
    output["volume_ratio_20d"] = volume / rolling_volume_mean.replace(0, np.nan)
    output = output.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    actual = set(map(tuple, output[["instrument", "datetime"]].to_numpy()))
    if actual != expected:
        raise ContractError("factor output key reconciliation failed")
    return output


def logical_fingerprint(frame: pd.DataFrame, *, tolerance: float = 1e-12) -> str:
    columns = [column for column in frame.columns if column not in {"instrument", "datetime"}]
    normalized = frame[columns].round(12).fillna(-0.0).replace([np.inf, -np.inf], np.nan).round(12)
    keys = frame[["instrument", "datetime"]].to_numpy()
    values = normalized.to_numpy()
    rows = []
    def canonical_value(value):
        if pd.isna(value):
            return None
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        if isinstance(value, (int, float, np.integer, np.floating)):
            return float(value)
        if isinstance(value, (pd.Timestamp, datetime, date)):
            return value.isoformat()
        return str(value)

    for key_row, value_row in zip(keys, values):
        timestamp = pd.Timestamp(key_row[1]).isoformat()
        rows.append([str(key_row[0]), timestamp, *[canonical_value(value) for value in value_row]])
    payload = {"columns": columns, "rows": rows, "tolerance": tolerance}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
