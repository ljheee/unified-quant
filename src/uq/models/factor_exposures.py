"""Point-in-time factor exposure contracts used by neutralized preprocessing."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..errors import ContractError

EXPOSURE_CONTROL_ORDER = ("industry", "market_cap", "liquidity")
EXPOSURE_DECISION_POLICY = "session_close_asia_shanghai_v1"
DECISION_ZONE = ZoneInfo("Asia/Shanghai")


def _session(value: object, *, label: str) -> str:
    if isinstance(value, pd.Timestamp):
        if value.tz is not None:
            raise ContractError(f"factor exposure {label} must be a local session date")
        value = value.strftime("%Y-%m-%d")
    elif isinstance(value, (date, datetime)):
        if isinstance(value, datetime) and value.tzinfo is not None:
            raise ContractError(f"factor exposure {label} must be a local session date")
        value = value.strftime("%Y-%m-%d")
    if not isinstance(value, str):
        raise ContractError(f"factor exposure {label} must be an ISO session date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"factor exposure {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ContractError(f"factor exposure {label} must be a canonical session date")
    return value


def _available_at(value: object, *, session: str) -> str:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("factor exposure available_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("factor exposure available_at must include a timezone")
    visible = parsed.tz_convert("UTC")
    session_close = pd.Timestamp(
        datetime.combine(date.fromisoformat(session), time(15, 0), tzinfo=DECISION_ZONE)
    ).tz_convert("UTC")
    if visible > session_close:
        raise ContractError(
            f"factor exposure was not visible by the decision close on {session}"
        )
    return visible.isoformat()


def normalize_exposure_panel(
    frame: pd.DataFrame,
    controls: list[str] | tuple[str, ...],
) -> pd.DataFrame:
    """Validate exposure rows and canonicalize session/availability timestamps."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ContractError("factor exposure panel must be a non-empty dataframe")
    control_names = list(controls)
    if (
        not control_names
        or len(control_names) != len(set(control_names))
        or control_names != [name for name in EXPOSURE_CONTROL_ORDER if name in control_names]
    ):
        raise ContractError("factor exposure controls must use unique canonical control order")
    expected_columns = ["instrument", "datetime", *control_names, "available_at"]
    if frame.columns.tolist() != expected_columns:
        raise ContractError("factor exposure columns do not match declared controls")
    result = frame.copy().reset_index(drop=True)
    if result[["instrument", "datetime", "available_at"]].isna().any().any():
        raise ContractError("factor exposure keys and availability cannot be null")
    if not result["instrument"].map(
        lambda item: isinstance(item, str) and bool(item.strip())
    ).all():
        raise ContractError("factor exposure instruments must be non-empty strings")
    result["datetime"] = [
        _session(value, label="datetime") for value in result["datetime"]
    ]
    if result.duplicated(["instrument", "datetime"]).any():
        raise ContractError("factor exposure panel contains duplicate keys")
    result["available_at"] = [
        _available_at(value, session=session)
        for value, session in zip(result["available_at"], result["datetime"])
    ]
    for control in control_names:
        values = result[control]
        if control == "industry":
            if not values.map(
                lambda item: isinstance(item, str) and bool(item.strip())
            ).all():
                raise ContractError("factor exposure industry labels must be non-empty strings")
            result[control] = values.str.strip()
            continue
        if not pd.api.types.is_numeric_dtype(values) or pd.api.types.is_bool_dtype(values):
            raise ContractError(f"factor exposure control must be numeric: {control}")
        numbers = values.to_numpy(dtype=float, na_value=np.nan)
        minimum_invalid = numbers < 0 if control == "liquidity" else numbers <= 0
        if not np.isfinite(numbers).all() or minimum_invalid.any():
            requirement = "non-negative" if control == "liquidity" else "positive"
            raise ContractError(
                f"factor exposure control must be finite and {requirement}: {control}"
            )
        result[control] = values.astype("float64")
    return result


def exposure_values_for_neutralization(
    frame: pd.DataFrame,
    *,
    controls: list[str] | tuple[str, ...],
) -> pd.DataFrame:
    """Return the exact PIT control frame expected by the OLS implementation."""
    normalized = normalize_exposure_panel(frame, controls)
    return normalized[["instrument", "datetime", *controls]].copy()


def align_exposure_panel(
    factor_frame: pd.DataFrame,
    exposure_panel: pd.DataFrame,
    controls: list[str] | tuple[str, ...],
) -> pd.DataFrame:
    """Select approved exposure rows for an exact factor key set."""
    if factor_frame.empty:
        raise ContractError("factor panel must be non-empty for exposure alignment")
    normalized = normalize_exposure_panel(exposure_panel, controls)
    keys = factor_frame[["instrument", "datetime"]].copy().reset_index(drop=True)
    key_dates: list[str] = []
    for value in keys["datetime"]:
        parsed = pd.Timestamp(value)
        if parsed.tzinfo is not None:
            raise ContractError("factor exposure alignment requires timezone-naive sessions")
        key_dates.append(parsed.strftime("%Y-%m-%d"))
    keys["datetime"] = key_dates
    keys["_factor_order"] = range(len(keys))
    joined = keys.merge(
        normalized[["instrument", "datetime", *controls, "available_at"]],
        on=["instrument", "datetime"],
        how="left",
        sort=False,
        validate="one_to_one",
        indicator=True,
    )
    if (joined["_merge"] != "both").any():
        missing = joined.loc[joined["_merge"] != "both", ["instrument", "datetime"]]
        raise ContractError(
            "factor exposure panel does not cover preprocessing keys: "
            f"{missing.head(5).to_dict(orient='records')}"
        )
    joined = joined.sort_values("_factor_order", kind="mergesort")
    return joined[["instrument", "datetime", *controls, "available_at"]].reset_index(drop=True)
