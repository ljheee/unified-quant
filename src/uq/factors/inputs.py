"""Governed projections from canonical bars partitions to factor input frames.

A factor run is bound to one canonical input line, but the factor frame contract
is line-independent: ``_validate_factor_frame`` always requires ``instrument``
plus ``datetime``.  This module is the single place allowed to turn a verified
canonical frame into that contract, so producers cannot each invent their own
rename or their own row-eligibility rule.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..errors import ContractError

TRADING_STATUS = "trading"
PROJECTABLE_BARS_SCHEMA_VERSIONS = ("research-v1", "v2")


def factor_input_frame(
    frame: pd.DataFrame,
    *,
    schema_version: str,
    required_columns: list[str] | tuple[str, ...],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Project a verified canonical bars frame onto a reviewed factor input.

    ``bars_daily.v2`` keeps the research-v1 units but differs in two ways that
    both have to be handled here rather than at the call site:

    * key column: ``session_date`` replaces ``datetime``;
    * row eligibility: ``v2`` can carry explicit non-trading rows
      (``status != "trading"``, zero volume), while ``research-v1`` simply had no
      row for a suspended instrument. Keeping such a row would silently change
      every rolling statistic -- ``volume_ratio_20d`` would report ``0.0``
      instead of being absent, and the 20-session means would absorb a zero --
      so the reviewed raw-price eligibility rule is trading rows only.

    Returns the projected frame and read-only statistics about what it did.
    """
    if schema_version not in PROJECTABLE_BARS_SCHEMA_VERSIONS:
        raise ContractError(
            f"no governed factor projection for bars_daily/{schema_version}; "
            f"projectable lines: {list(PROJECTABLE_BARS_SCHEMA_VERSIONS)}"
        )
    source_rows = int(len(frame))
    projected = frame
    excluded = 0
    if schema_version == "v2":
        if "session_date" not in projected.columns:
            raise ContractError("bars_daily/v2 input frame is missing session_date")
        if "status" in projected.columns:
            status = projected["status"].astype(str)
            excluded = int((status != TRADING_STATUS).sum())
            projected = projected.loc[status == TRADING_STATUS]
        projected = projected.rename(columns={"session_date": "datetime"})

    missing = sorted(set(required_columns) - set(projected.columns))
    if missing:
        raise ContractError(f"factor input frame is missing reviewed columns: {missing}")
    projected = projected[list(required_columns)].copy()
    projected["datetime"] = pd.to_datetime(projected["datetime"])
    if projected[["instrument", "datetime"]].isna().any().any():
        raise ContractError("factor input keys cannot be null")
    if projected.duplicated(["instrument", "datetime"]).any():
        raise ContractError("duplicate factor input keys reject projection")
    statistics = {
        "source_schema_version": schema_version,
        "source_rows": source_rows,
        "non_trading_rows_excluded": excluded,
        "projected_rows": int(len(projected)),
    }
    return projected.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True), statistics
