from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from ..errors import ContractError


_REQUIRED = ["instrument", "datetime", "close", "adj_factor"]
_LINEAGE_VERSION = "adj_factor.exchange_v1"


def _validate_binding(frame: pd.DataFrame, adjustment_snapshot_id: str, effective_date_table_checksum: str) -> None:
    missing = sorted(set(_REQUIRED) - set(frame.columns))
    if missing:
        raise ContractError(f"adjusted dependency absent: {missing}")
    if frame[["instrument", "datetime", "adj_factor"]].isna().any().any():
        raise ContractError("adjusted dependency contains null values")
    if frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("duplicate canonical keys reject adjusted factor calculation")
    if not (frame["adj_factor"] > 0).all():
        raise ContractError("invalid non-positive adjustment factor")
    if not adjustment_snapshot_id or len(adjustment_snapshot_id) != 64:
        raise ContractError("adjustment lineage version mismatch")
    if not effective_date_table_checksum or len(effective_date_table_checksum) != 64:
        raise ContractError("effective-date table checksum mismatch")


def calculate_adjusted_factors(
    frame: pd.DataFrame,
    *,
    adjustment_snapshot_ids: list[str],
    effective_date_table_checksums: list[str],
) -> pd.DataFrame:
    _validate_binding(frame, adjustment_snapshot_ids[0], effective_date_table_checksums[0])
    unique_snapshots = set(adjustment_snapshot_ids)
    unique_tables = set(effective_date_table_checksums)
    if len(unique_snapshots) != 1:
        raise ContractError("two snapshots inside one return/volatility window")
    if len(unique_tables) != 1:
        raise ContractError("effective-date table checksum mismatch inside one window")
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    expected_keys = set(map(tuple, ordered[["instrument", "datetime"]].to_numpy()))
    output = ordered[["instrument", "datetime"]].copy()
    adjusted_close = ordered["close"].astype(float) * ordered["adj_factor"].astype(float)

    def per_instrument(series: pd.Series, periods: int, operation: str = "return") -> pd.Series:
        return series.groupby(ordered["instrument"], sort=False).transform(
            lambda values: values.pct_change(periods) if operation == "return" else values.rolling(periods, min_periods=periods).std(ddof=1)
        )

    output["return_1d"] = per_instrument(adjusted_close, 1)
    output["return_5d"] = per_instrument(adjusted_close, 5)
    output["return_20d"] = per_instrument(adjusted_close, 20)
    output["volatility_20d"] = per_instrument(adjusted_close.pct_change(1), 20, "volatility")
    output = output.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    if set(map(tuple, output[["instrument", "datetime"]].to_numpy())) != expected_keys:
        raise ContractError("factor output key reconciliation failed")
    return output


def assert_not_raw_close(frame: pd.DataFrame, result: pd.DataFrame) -> None:
    if np.allclose(result["return_1d"].dropna(), frame.sort_values(["instrument","datetime"])["close"].pct_change().dropna(), atol=1e-15, equal_nan=True):
        raise ContractError("raw close substituted for adjusted close")
