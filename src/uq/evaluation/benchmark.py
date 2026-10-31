"""Construct point-in-time benchmark target weights for the backtest engine.

The caller supplies membership and exposure frames read from their owning,
reviewed stores. This module deliberately does not infer historical membership,
market capitalization, or industry from current values.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..contracts.index_membership import (
    IndexMembershipStore,
    build_membership_set,
    verify_membership_set,
)
from .metrics import session_dates

BenchmarkScheme = Literal["equal_weight", "market_cap_weighted", "industry_neutral"]


def read_pit_benchmark_membership(
    store: IndexMembershipStore,
    *,
    universe_id: str,
    calendar_dates: list[str],
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, dict]:
    """Read daily constituents and their snapshot lineage from the owning store.

    The membership store currently records monthly-approximation PIT snapshots.
    This helper applies its `visible_from` rule for every governed session and
    returns the exact membership-set receipt used to derive the rows. It does
    not upgrade that source's monthly precision into daily historical truth.
    """
    calendar = session_dates(calendar_dates, label="benchmark calendar")
    if not calendar or calendar != sorted(set(calendar)):
        raise ContractError("benchmark calendar must be nonempty, sorted and unique")
    window = session_dates([start_date, end_date], label="benchmark window")
    start, end = window
    if start > end or start not in calendar or end not in calendar:
        raise ContractError("benchmark window endpoints must be ordered calendar sessions")
    days = [date.fromisoformat(day) for day in calendar if start <= day <= end]
    if not days or days[0].isoformat() != start or days[-1].isoformat() != end:
        raise ContractError("benchmark calendar does not cover the complete requested window")

    membership_set = build_membership_set(
        store,
        universe_id=universe_id,
        window_start=days[0],
        window_end=days[-1],
        trading_days=days,
    )
    verify_membership_set(store, membership_set, days)
    frames: list[pd.DataFrame] = []
    for day in days:
        snapshot_date, members = store.as_of(universe_id, day)
        manifest = store.read_manifest(universe_id, snapshot_date)
        if manifest["index_code"] != membership_set["index_code"]:
            raise ContractError("benchmark membership source mixes index codes")
        frame = members.copy()
        frame.insert(0, "date", day.isoformat())
        frames.append(frame)
    membership = _validate_rows(
        pd.concat(frames, ignore_index=True),
        columns=["date", "instrument"],
        label="benchmark membership",
    )
    return membership, membership_set


def _validate_rows(frame: pd.DataFrame, *, columns: list[str], label: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ContractError(f"{label} input must be a non-empty DataFrame")
    if frame.columns.tolist() != columns:
        raise ContractError(f"{label} columns must be exactly {columns}")
    result = frame.copy()
    if result[["date", "instrument"]].isna().any().any():
        raise ContractError(f"{label} keys cannot be null")
    if result.duplicated(["date", "instrument"]).any():
        raise ContractError(f"{label} has duplicate date/instrument keys")
    normalized_dates: list[str] = []
    for value in result["date"]:
        if not isinstance(value, str):
            raise ContractError(f"{label} dates must be ISO date strings")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ContractError(f"{label} has an invalid session date") from exc
        if parsed.isoformat() != value:
            raise ContractError(f"{label} dates must use canonical ISO format")
        normalized_dates.append(value)
    result["date"] = normalized_dates
    if not result["instrument"].map(
        lambda value: isinstance(value, str) and bool(value.strip())
    ).all():
        raise ContractError(f"{label} instruments must be non-empty strings")
    return result


def _exact_exposure(
    membership: pd.DataFrame,
    exposure: pd.DataFrame,
    *,
    label: str,
) -> pd.DataFrame:
    keys = ["date", "instrument"]
    expected = membership[keys].sort_values(keys, kind="mergesort").reset_index(drop=True)
    actual = exposure[keys].sort_values(keys, kind="mergesort").reset_index(drop=True)
    missing = expected.merge(actual, on=keys, how="left", indicator=True)
    missing = missing.loc[missing["_merge"] == "left_only", keys]
    if not missing.empty:
        first = missing.iloc[0]
        raise ContractError(
            f"{label} is missing point-in-time exposure for {first['instrument']} on {first['date']}"
        )
    return membership[keys].merge(exposure, on=keys, how="left", validate="one_to_one")


def build_benchmark_weight_partitions(
    membership: pd.DataFrame,
    *,
    scheme: BenchmarkScheme,
    market_cap: pd.DataFrame | None = None,
    industry: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Build daily target weights from exact point-in-time membership.

    Args:
        membership: exact columns ``[date, instrument]``. It must contain the
            complete membership set for every benchmark rebalance session.
        scheme: equal weight, market-cap weight, or equal industry budgets with
            equal weights inside each industry.
        market_cap: exact columns ``[date, instrument, market_cap]`` for every
            member/session; required for ``market_cap_weighted``. Values must be
            finite and strictly positive.
        industry: exact columns ``[date, instrument, industry]`` for every
            member/session; required for ``industry_neutral``. Labels must be
            non-empty strings.

    Returns:
        Mapping from decision-session ISO date to ``[instrument, weight]`` rows.
        These partitions can be passed directly to ``BacktestEngine.run``. The
        engine then applies the reviewed config's costs, execution rules, and
        risk controls to both the benchmark and strategy.

    Extra rows in an exposure frame are ignored after key validation; missing
    rows for any constituent fail closed. PIT correctness and source review are
    the responsibility of the owning-store readers that supply these frames.
    """
    if scheme not in {"equal_weight", "market_cap_weighted", "industry_neutral"}:
        raise ContractError(f"unsupported benchmark weighting scheme: {scheme}")
    members = _validate_rows(
        membership, columns=["date", "instrument"], label="benchmark membership"
    )
    if members.empty:
        raise ContractError("benchmark membership is empty")

    caps = None
    if scheme == "market_cap_weighted":
        if market_cap is None:
            raise ContractError("market-cap benchmark requires point-in-time market cap")
        caps = _validate_rows(
            market_cap,
            columns=["date", "instrument", "market_cap"],
            label="benchmark market cap",
        )
        if (not pd.api.types.is_numeric_dtype(caps["market_cap"])
                or pd.api.types.is_bool_dtype(caps["market_cap"])):
            raise ContractError("benchmark market cap must be numeric, not boolean")
        try:
            values = caps["market_cap"].to_numpy(dtype=float)
        except (TypeError, ValueError) as exc:
            raise ContractError("benchmark market cap must be numeric") from exc
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ContractError("benchmark market cap must be finite and positive")
        caps["market_cap"] = values
        caps = _exact_exposure(members, caps, label="market cap")

    industries = None
    if scheme == "industry_neutral":
        if industry is None:
            raise ContractError("industry-neutral benchmark requires point-in-time industry")
        industries = _validate_rows(
            industry,
            columns=["date", "instrument", "industry"],
            label="benchmark industry",
        )
        if not industries["industry"].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        ).all():
            raise ContractError("benchmark industry labels must be non-empty strings")
        industries = _exact_exposure(members, industries, label="industry")
        industries["industry"] = industries["industry"].map(str.strip)

    weights: dict[str, pd.DataFrame] = {}
    for decision_date, day_members in members.groupby("date", sort=True):
        instruments = day_members["instrument"].tolist()
        count = len(instruments)
        if count == 0:
            raise ContractError(f"benchmark membership is empty on {decision_date}")
        frame = pd.DataFrame({"instrument": instruments})
        if scheme == "equal_weight":
            frame["weight"] = 1.0 / count
        elif scheme == "market_cap_weighted":
            if caps is None:
                raise ContractError("market-cap benchmark requires point-in-time market cap")
            day_caps = caps.loc[caps["date"] == decision_date, ["instrument", "market_cap"]]
            aligned = frame.merge(day_caps, on="instrument", how="left", validate="one_to_one")
            total = float(aligned["market_cap"].sum())
            if not np.isfinite(total) or total <= 0:
                raise ContractError(f"market-cap total is invalid on {decision_date}")
            frame["weight"] = aligned["market_cap"].to_numpy(dtype=float) / total
        else:
            if industries is None:
                raise ContractError("industry-neutral benchmark requires point-in-time industry")
            day_industries = industries.loc[
                industries["date"] == decision_date, ["instrument", "industry"]
            ]
            aligned = frame.merge(
                day_industries, on="instrument", how="left", validate="one_to_one"
            )
            counts = aligned.groupby("industry")["instrument"].transform("count")
            industry_count = int(aligned["industry"].nunique())
            frame["weight"] = 1.0 / (industry_count * counts.to_numpy(dtype=float))

        frame = frame.sort_values("instrument", kind="mergesort").reset_index(drop=True)
        values = frame["weight"].to_numpy(dtype=float)
        if (not np.isfinite(values).all() or (values < 0).any()
                or not np.isclose(float(values.sum()), 1.0, rtol=0, atol=1e-12)):
            raise ContractError(f"benchmark weights do not normalize on {decision_date}")
        weights[decision_date] = frame
    return weights


def _weight_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    ordered = frame.sort_values(["date", "instrument"], kind="mergesort")
    return [
        {"date": str(row.date), "instrument": str(row.instrument), "weight": float(row.weight)}
        for row in ordered.itertuples(index=False)
    ]


def _membership_panel_fingerprint(frame: pd.DataFrame) -> str:
    normalized = _validate_rows(
        frame, columns=["date", "instrument"], label="benchmark membership panel"
    ).sort_values(["date", "instrument"], kind="mergesort")
    rows = [
        {"date": str(row.date), "instrument": str(row.instrument)}
        for row in normalized.itertuples(index=False)
    ]
    return sha256_json(rows)


def _benchmark_partition_bindings(
    frame: pd.DataFrame,
    *,
    scheme: str,
    membership_digest: str,
    exposure_binding: dict | None,
    config_generation_id: str,
) -> list[dict[str, object]]:
    result = []
    for decision_date, day in frame.groupby("date", sort=True):
        rows = _weight_records(day)
        data_checksum = sha256_json(rows)
        generation_id = sha256_json({
            "decision_date": decision_date,
            "scheme": scheme,
            "rows": rows,
            "membership_set_digest_sha256": membership_digest,
            "exposure_binding": exposure_binding,
            "backtest_config_generation_id": config_generation_id,
        })
        digest_payload = {
            "decision_date": decision_date,
            "generation_id": generation_id,
            "data_checksum_sha256": data_checksum,
            "row_count": len(rows),
        }
        result.append({
            **digest_payload,
            "manifest_digest_sha256": sha256_json(digest_payload),
        })
    return result


class BenchmarkWeightStore:
    """Publish immutable benchmark weights bound to PIT sources and a strategy schedule."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.panel_root = self.root / "benchmark_weight_panels"

    def publish(
        self,
        weights: dict[str, pd.DataFrame],
        *,
        scheme: BenchmarkScheme,
        universe_id: str,
        membership_set: dict,
        membership_panel: pd.DataFrame,
        backtest_config_binding: dict[str, str],
        strategy_schedule_binding: dict,
        strategy_universe_binding: dict,
        terminal_liquidation: bool,
        exposure_binding: dict | None,
    ) -> dict:
        if scheme not in {"equal_weight", "market_cap_weighted", "industry_neutral"}:
            raise ContractError(f"unsupported benchmark weighting scheme: {scheme}")
        if scheme != "equal_weight":
            required_control = "market_cap" if scheme == "market_cap_weighted" else "industry"
            if (not isinstance(exposure_binding, dict)
                    or required_control not in exposure_binding.get("controls", [])):
                raise ContractError(f"{scheme} benchmark lacks its reviewed PIT exposure binding")
        membership_digest = membership_set.get("set_digest_sha256")
        if (not isinstance(membership_digest, str)
                or membership_digest != sha256_json({
                    key: value for key, value in membership_set.items()
                    if key != "set_digest_sha256"
                })):
            raise ContractError("benchmark membership-set digest is invalid")
        if membership_set.get("universe_id") != universe_id:
            raise ContractError("benchmark membership universe does not match request")
        config_generation = backtest_config_binding.get("generation_id")
        if not isinstance(config_generation, str) or len(config_generation) != 64:
            raise ContractError("benchmark backtest config binding is invalid")
        schedule = strategy_schedule_binding.get("decision_dates")
        if (not isinstance(schedule, list) or not schedule
                or schedule != sorted(set(schedule))
                or set(schedule) != set(weights)):
            raise ContractError("benchmark weights must match the ordered strategy decision schedule")
        if strategy_schedule_binding.get("manifest_digest_sha256") is None:
            raise ContractError("benchmark strategy schedule lacks a manifest digest")
        if strategy_universe_binding.get("universe_id") != universe_id:
            raise ContractError("benchmark strategy and membership universe ids differ")
        if type(terminal_liquidation) is not bool:
            raise ContractError("benchmark terminal_liquidation must be a boolean")
        normalized_membership = _validate_rows(
            membership_panel,
            columns=["date", "instrument"],
            label="benchmark membership panel",
        )
        if not set(schedule) <= set(normalized_membership["date"]):
            raise ContractError("benchmark membership panel does not cover the decision schedule")

        rows = []
        for day in schedule:
            frame = weights[day]
            if frame.columns.tolist() != ["instrument", "weight"] or frame.empty:
                raise ContractError(f"benchmark weight partition has invalid columns or no rows: {day}")
            current = frame.copy()
            if current["instrument"].duplicated().any():
                raise ContractError(f"benchmark weight partition has duplicate instruments: {day}")
            if not current["instrument"].map(
                lambda value: isinstance(value, str) and bool(value.strip())
            ).all():
                raise ContractError(f"benchmark weight partition has invalid instruments: {day}")
            try:
                values = current["weight"].to_numpy(dtype=float)
            except (TypeError, ValueError) as exc:
                raise ContractError(f"benchmark weights are non-numeric on {day}") from exc
            if (not np.isfinite(values).all() or (values < 0).any()
                    or not np.isclose(float(values.sum()), 1.0, rtol=0, atol=1e-12)):
                raise ContractError(f"benchmark weights must be finite and sum to one on {day}")
            rows.extend({
                "date": day,
                "instrument": str(instrument),
                "weight": float(weight),
            } for instrument, weight in zip(current["instrument"], values))
        combined = pd.DataFrame(rows, columns=["date", "instrument", "weight"])
        combined = combined.sort_values(["date", "instrument"], kind="mergesort").reset_index(drop=True)
        if combined.duplicated(["date", "instrument"]).any():
            raise ContractError("benchmark weights contain duplicate date/instrument keys")
        member_keys = set(zip(normalized_membership["date"], normalized_membership["instrument"]))
        scheduled_member_keys = {
            key for key in member_keys if key[0] in set(schedule)
        }
        weight_keys = set(zip(combined["date"], combined["instrument"]))
        if weight_keys != scheduled_member_keys:
            raise ContractError(
                "benchmark weights must cover the exact PIT membership on every decision date"
            )

        # A membership-set names snapshots, while the daily rows are reconstructed
        # by its owning reader before this publisher is called. Keep the set digest
        # in the manifest; each weight partition remains bound to that exact set.
        if not isinstance(membership_set.get("snapshots"), list) or not membership_set["snapshots"]:
            raise ContractError("benchmark membership set has no governed snapshots")
        data = io.BytesIO()
        combined.to_parquet(data, index=False, compression="snappy")
        data_bytes = data.getvalue()
        records = _weight_records(combined)
        partitions = _benchmark_partition_bindings(
            combined,
            scheme=scheme,
            membership_digest=membership_digest,
            exposure_binding=exposure_binding,
            config_generation_id=config_generation,
        )
        manifest = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "scheme": scheme,
            "universe_id": universe_id,
            "index_code": membership_set["index_code"],
            "window_start": membership_set["window_start"],
            "window_end": membership_set["window_end"],
            "backtest_config_binding": dict(backtest_config_binding),
            "strategy_schedule_binding": dict(strategy_schedule_binding),
            "strategy_universe_binding": dict(strategy_universe_binding),
            "terminal_liquidation": terminal_liquidation,
            "membership_set": dict(membership_set),
            "membership_panel_checksum_sha256": _membership_panel_fingerprint(normalized_membership),
            "exposure_binding": dict(exposure_binding) if exposure_binding is not None else None,
            "decision_dates": list(schedule),
            "partitions": partitions,
            "weights_file": "weights.parquet",
            "weights_checksum_sha256": file_sha256_bytes(data_bytes),
            "weights_logical_fingerprint": sha256_json(records),
            "row_count": len(combined),
            "columns": combined.columns.tolist(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation, digest = model_manifest_identities(
            manifest,
            schema_name="benchmark_weight_panel",
        )
        manifest["generation_id"] = generation
        manifest["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("benchmark_weight_panel", manifest)

        self.root.mkdir(parents=True, exist_ok=True)
        self.panel_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".benchmark_weight_publish.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            destination = self.panel_root / f"generation={generation}"
            if destination.exists():
                existing, _ = self.read(generation, membership_panel=normalized_membership)
                if existing != manifest:
                    raise ContractError("benchmark weight generation conflicts with existing panel")
                return existing
            reject_symlinks_within(self.root, destination, label="benchmark weight store path")
            staging = self.panel_root / f".staging_{uuid.uuid4().hex}"
            staging.mkdir()
            try:
                (staging / "weights.parquet").write_bytes(data_bytes)
                (staging / "manifest.json").write_text(
                    json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                fsync_tree(staging)
                os.replace(staging, destination)
                fsync_dir(self.panel_root)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            return manifest

    def read(
        self,
        generation_id: str,
        *,
        membership_panel: pd.DataFrame | None = None,
    ) -> tuple[dict, dict[str, pd.DataFrame]]:
        if (not isinstance(generation_id, str) or len(generation_id) != 64
                or any(char not in "0123456789abcdef" for char in generation_id)):
            raise ContractError("invalid benchmark weight generation id")
        directory = self.panel_root / f"generation={generation_id}"
        reject_symlinks_within(self.root, directory, label="benchmark weight store path")
        manifest_path = directory / "manifest.json"
        data_path = directory / "weights.parquet"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"unpublished benchmark weight panel: {generation_id}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("benchmark weight manifest is unavailable or malformed") from exc
        ModelContractLoader.validate("benchmark_weight_panel", manifest)
        membership_set = manifest.get("membership_set")
        if (
            not isinstance(membership_set, dict)
            or membership_set.get("set_digest_sha256") != sha256_json({
                key: value for key, value in membership_set.items()
                if key != "set_digest_sha256"
            })
            or membership_set.get("universe_id") != manifest.get("universe_id")
            or membership_set.get("index_code") != manifest.get("index_code")
            or membership_set.get("window_start") != manifest.get("window_start")
            or membership_set.get("window_end") != manifest.get("window_end")
        ):
            raise ContractError("benchmark membership-set identity mismatch")
        if (
            manifest.get("strategy_schedule_binding", {}).get("decision_dates")
            != manifest.get("decision_dates")
            or manifest.get("strategy_universe_binding", {}).get("universe_id")
            != manifest.get("universe_id")
        ):
            raise ContractError("benchmark strategy schedule or universe binding mismatch")
        expected = model_manifest_identities(
            manifest,
            schema_name="benchmark_weight_panel",
        )
        if expected != (generation_id, manifest["manifest_digest_sha256"]):
            raise ContractError("benchmark weight manifest identity mismatch")
        data_bytes = data_path.read_bytes()
        if file_sha256_bytes(data_bytes) != manifest["weights_checksum_sha256"]:
            raise ContractError("benchmark weight data checksum mismatch")
        try:
            frame = pd.read_parquet(io.BytesIO(data_bytes))
        except (ValueError, OSError) as exc:
            raise ContractError("benchmark weight parquet is unreadable") from exc
        if frame.columns.tolist() != manifest["columns"] or len(frame) != manifest["row_count"]:
            raise ContractError("benchmark weight panel shape mismatch")
        if frame.duplicated(["date", "instrument"]).any():
            raise ContractError("benchmark weight panel has duplicate keys")
        if not frame["date"].map(
            lambda value: isinstance(value, str)
            and value in manifest["decision_dates"]
        ).all():
            raise ContractError("benchmark weight panel contains an invalid session date")
        if not frame["instrument"].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        ).all():
            raise ContractError("benchmark weight panel contains an invalid instrument")
        if (membership_panel is not None
                and _membership_panel_fingerprint(membership_panel)
                != manifest["membership_panel_checksum_sha256"]):
            raise ContractError("benchmark membership panel binding mismatch")
        try:
            values = frame["weight"].to_numpy(dtype=float)
        except (TypeError, ValueError) as exc:
            raise ContractError("benchmark weight panel contains non-numeric values") from exc
        if not np.isfinite(values).all() or (values < 0).any():
            raise ContractError("benchmark weight panel contains invalid weights")
        if sha256_json(_weight_records(frame)) != manifest["weights_logical_fingerprint"]:
            raise ContractError("benchmark weight logical fingerprint mismatch")
        date_groups = {day: group.drop(columns="date").reset_index(drop=True)
                       for day, group in frame.groupby("date", sort=True)}
        if sorted(date_groups) != manifest["decision_dates"]:
            raise ContractError("benchmark weight decision dates mismatch")
        for day, part in date_groups.items():
            if not np.isclose(float(part["weight"].sum()), 1.0, rtol=0, atol=1e-12):
                raise ContractError(f"benchmark weights do not sum to one on {day}")
        if membership_panel is not None:
            members = _validate_rows(
                membership_panel,
                columns=["date", "instrument"],
                label="benchmark membership panel",
            )
            member_keys = {
                (day, instrument)
                for day, instrument in zip(members["date"], members["instrument"])
                if day in manifest["decision_dates"]
            }
            weight_keys = {
                (day, instrument)
                for day, part in date_groups.items()
                for instrument in part["instrument"]
            }
            if weight_keys != member_keys:
                raise ContractError("benchmark panel read does not exactly cover PIT membership")
        expected_parts = _benchmark_partition_bindings(
            frame,
            scheme=manifest["scheme"],
            membership_digest=manifest["membership_set"]["set_digest_sha256"],
            exposure_binding=manifest["exposure_binding"],
            config_generation_id=manifest["backtest_config_binding"]["generation_id"],
        )
        if expected_parts != manifest["partitions"]:
            raise ContractError("benchmark weight partition bindings mismatch")
        return manifest, date_groups
