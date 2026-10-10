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


# cr.md U2: the fingerprint algorithm has changed twice (H11 null-mask, U18
# infinity signs), which orphaned every artifact pinned by an older algorithm.
# Rather than cut existing partitions off, reads accept any known version
# (verify_logical_fingerprint below); writes always use the latest.
KNOWN_FINGERPRINT_VERSIONS = (1, 2, 3)
LATEST_FINGERPRINT_VERSION = 3


def logical_fingerprint(frame: pd.DataFrame, *, tolerance: float = 1e-12, version: int = LATEST_FINGERPRINT_VERSION) -> str:
    columns = [column for column in frame.columns if column not in {"instrument", "datetime"}]
    if version == 1:
        # Legacy (pre-H11): masking NaN to -0.0 made missing values collide
        # with genuine -0.0; kept for reading存量 artifacts.
        normalized = frame[columns].round(12).fillna(-0.0).replace([np.inf, -np.inf], np.nan).round(12)
        null_mask = None
    elif version == 2:
        # cr.md H11: masking NaN to -0.0 used to make missing values collide
        # with genuine -0.0 in the fingerprint; hash the null mask
        # independently and keep NaN distinct from both -0.0 and masked
        # infinities.
        null_mask = frame[columns].isna().to_numpy(dtype=bool)
        normalized = frame[columns].round(12).replace([np.inf, -np.inf], np.nan).round(12)
    elif version == 3:
        # cr.md U18/U24: version 2 mapped both infinities to NaN *after*
        # sampling the null mask, so +inf and -inf still collided with each
        # other; emit them as distinct signed sentinels instead.
        null_mask = frame[columns].isna().to_numpy(dtype=bool)
        normalized = frame[columns].round(12)
    else:
        raise ContractError(f"unsupported logical fingerprint version: {version}")
    keys = frame[["instrument", "datetime"]].to_numpy()
    values = normalized.to_numpy()
    rows = []
    def canonical_value(value):
        if pd.isna(value):
            return None
        if version == 3 and isinstance(value, (float, np.floating)):
            if value == np.inf:
                return "+inf"
            if value == -np.inf:
                return "-inf"
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
    if null_mask is not None:
        payload["null_mask"] = null_mask.tolist()
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def verify_logical_fingerprint(frame: pd.DataFrame, expected: str | None, *, tolerance: float = 1e-12) -> bool:
    """Accept a fingerprint pinned by any known algorithm version.

    cr.md U2 migration layer: manifests carry no algorithm marker, so a stored
    fingerprint may come from any historical version; every known version is a
    deterministic, key-inclusive SHA-256 construction, so multi-accept does not
    weaken tamper evidence in practice.
    """
    if not expected:
        return False
    return any(
        logical_fingerprint(frame, tolerance=tolerance, version=version) == expected
        for version in KNOWN_FINGERPRINT_VERSIONS
    )
