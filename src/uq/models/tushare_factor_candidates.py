"""Build auditable, diagnostic-only factor and exposure candidates from raw Tushare snapshots.

The source responses are historical backfills without provider transaction-time
vintages. This module records an explicit next-session visibility assumption and
marks every bundle ineligible for strict PIT promotion; callers must not publish
it as production evidence or treat it as proof of point-in-time correctness.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.gate_contracts import sha256_json, validate_contract
from ..contracts.manifest import load_canonical_manifest
from ..contracts.paths import reject_symlinks_within
from ..contracts.tushare_dependency import TushareDependencyStore
from ..errors import ContractError
from ..factors.repro_staging import environment_profile
from .factor_exposures import normalize_exposure_panel


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EXCHANGE_SUFFIXES = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}
_FACTOR_COLUMNS = (
    "earnings_yield_ttm",
    "book_to_price",
    "sales_yield_ttm",
    "dividend_yield_ttm",
    "turnover_rate_free_float",
    "volume_ratio",
    "moneyflow_net_to_market_cap",
    "large_order_net_to_market_cap",
)
_FACTOR_DEFINITIONS = (
    {"name": "earnings_yield_ttm", "version": "1.0.0",
     "sources": [{"dataset": "daily_basic", "fields": ["pe_ttm"]}],
     "formula": "1 / pe_ttm when pe_ttm > 0; otherwise null"},
    {"name": "book_to_price", "version": "1.0.0",
     "sources": [{"dataset": "daily_basic", "fields": ["pb"]}],
     "formula": "1 / pb when pb > 0; otherwise null"},
    {"name": "sales_yield_ttm", "version": "1.0.0",
     "sources": [{"dataset": "daily_basic", "fields": ["ps_ttm"]}],
     "formula": "1 / ps_ttm when ps_ttm > 0; otherwise null"},
    {"name": "dividend_yield_ttm", "version": "1.0.0",
     "sources": [{"dataset": "daily_basic", "fields": ["dv_ttm"]}],
     "formula": "dv_ttm / 100; source is percent"},
    {"name": "turnover_rate_free_float", "version": "1.0.0",
     "sources": [{"dataset": "daily_basic", "fields": ["turnover_rate_f"]}],
     "formula": "provider percent value, unchanged"},
    {"name": "volume_ratio", "version": "1.0.0",
     "sources": [{"dataset": "daily_basic", "fields": ["volume_ratio"]}],
     "formula": "provider value, unchanged"},
    {"name": "moneyflow_net_to_market_cap", "version": "1.0.0",
     "sources": [
         {"dataset": "daily_basic", "fields": ["total_mv"]},
         {"dataset": "moneyflow", "fields": ["net_mf_amount"]},
     ],
     "formula": "net_mf_amount / total_mv; both provider fields are in 10k CNY"},
    {"name": "large_order_net_to_market_cap", "version": "1.0.0",
     "sources": [
         {"dataset": "daily_basic", "fields": ["total_mv"]},
         {"dataset": "moneyflow", "fields": ["buy_lg_amount", "buy_elg_amount", "sell_lg_amount", "sell_elg_amount"]},
     ],
     "formula": "(buy_lg + buy_elg - sell_lg - sell_elg) / total_mv; amounts in 10k CNY"},
)
_EXPOSURE_DEFINITIONS = (
    {"control": "market_cap", "source_dataset": "daily_basic", "source_field": "total_mv",
     "units": "10k CNY, provider total market value"},
    {"control": "liquidity", "source_dataset": "daily_basic", "source_field": "turnover_rate_f",
     "units": "percent, free-float turnover rate"},
)


@dataclass(frozen=True)
class TushareFactorCandidateBundle:
    factors: pd.DataFrame
    exposures: pd.DataFrame
    manifest: dict[str, Any]


def _trade_date(value: Any, *, label: str) -> date:
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    try:
        result = date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{label} must be a valid YYYYMMDD or YYYY-MM-DD date") from exc
    return result


def _instrument(value: Any) -> str:
    if not isinstance(value, str) or value.count(".") != 1:
        raise ContractError(f"invalid Tushare instrument code: {value!r}")
    code, exchange = value.split(".")
    suffix = _EXCHANGE_SUFFIXES.get(exchange)
    if len(code) != 6 or not code.isdigit() or suffix is None:
        raise ContractError(f"unsupported Tushare instrument code: {value!r}")
    return f"{code}.{suffix}"


def _number_column(frame: pd.DataFrame, name: str, *, allow_null: bool = True) -> pd.Series:
    if name not in frame:
        raise ContractError(f"Tushare source panel is missing field: {name}")
    values = pd.to_numeric(frame[name], errors="coerce")
    raw_non_null = frame[name].notna()
    if (raw_non_null & values.isna()).any():
        raise ContractError(f"Tushare source field is not numeric: {name}")
    array = values.to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(array).any() or (not allow_null and not np.isfinite(array).all()):
        raise ContractError(f"Tushare source field contains invalid values: {name}")
    return values.astype("float64")


def _query_date(manifest: Mapping[str, Any]) -> date:
    query = manifest.get("query")
    if not isinstance(query, dict) or set(query) != {"trade_date"}:
        raise ContractError("PIT candidate builder requires one exact trade_date query per raw snapshot")
    return _trade_date(query["trade_date"], label="raw query trade_date")


def _latest_daily_snapshot_map(
    dependency_store: TushareDependencyStore, dataset: str
) -> dict[date, dict[str, Any]]:
    grouped: dict[date, list[dict[str, Any]]] = {}
    for manifest in dependency_store.list_manifests(dataset):
        day = _query_date(manifest)
        grouped.setdefault(day, []).append(manifest)
    return {
        day: max(items, key=lambda item: (item["source_fetched_at"], item["generation_id"]))
        for day, items in grouped.items()
    }


def _canonical_calendar(bars_root: Path) -> tuple[list[date], list[dict[str, str]]]:
    partition_root = bars_root / "canonical" / "bars_daily" / "v2"
    if not partition_root.is_dir():
        raise ContractError(f"canonical bars_daily/v2 calendar is unavailable under {bars_root}")
    dates: list[date] = []
    bindings: list[dict[str, str]] = []
    for directory in sorted(partition_root.glob("date=*")):
        if not directory.is_dir() or directory.is_symlink():
            continue
        day = _trade_date(directory.name.removeprefix("date="), label="calendar partition")
        manifest_path = directory / "manifest.json"
        data_path = directory / "data.parquet"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"canonical calendar partition is incomplete: {day.isoformat()}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"canonical calendar manifest is unreadable: {day.isoformat()}") from exc
        load_canonical_manifest(manifest)
        checksum = file_sha256_bytes(data_path.read_bytes())
        if (
            manifest.get("dataset") != "bars_daily"
            or manifest.get("schema_version") != "v2"
            or manifest.get("partition_date") != day.isoformat()
            or manifest.get("data_checksum_sha256") != checksum
        ):
            raise ContractError(f"canonical calendar partition identity/checksum mismatch: {day.isoformat()}")
        dates.append(day)
        bindings.append({
            "session_date": day.isoformat(),
            "manifest_generation_id": str(manifest.get("generation_id", "")),
            "data_checksum_sha256": checksum,
        })
    if not dates or dates != sorted(set(dates)):
        raise ContractError("canonical bars calendar must be non-empty, sorted, and unique")
    if any(len(item["manifest_generation_id"]) != 64 for item in bindings):
        raise ContractError("canonical calendar manifest generation is invalid")
    return dates, bindings


def _read_raw_partition(
    dependency_store: TushareDependencyStore,
    snapshot: dict[str, Any],
    *,
    dataset: str,
    expected_day: date,
) -> pd.DataFrame:
    manifest, frame = dependency_store.read_frame(snapshot["generation_id"], dataset=dataset)
    if manifest["generation_id"] != snapshot["generation_id"]:
        raise ContractError("Tushare snapshot changed while building factor candidates")
    if _query_date(manifest) != expected_day or frame.empty:
        raise ContractError(f"{dataset} snapshot does not cover {expected_day.isoformat()}")
    if "trade_date" not in frame:
        raise ContractError(f"{dataset} snapshot lacks trade_date")
    observed_dates = {_trade_date(item, label=f"{dataset} trade_date") for item in frame["trade_date"]}
    if observed_dates != {expected_day}:
        raise ContractError(f"{dataset} snapshot contains rows outside {expected_day.isoformat()}")
    if "ts_code" not in frame or frame["ts_code"].isna().any():
        raise ContractError(f"{dataset} snapshot has invalid instrument keys")
    if frame["ts_code"].duplicated().any():
        raise ContractError(f"{dataset} snapshot has duplicate instrument rows")
    return frame.copy()


def build_tushare_factor_candidates(
    *,
    source_root: Path | str,
    bars_root: Path | str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> TushareFactorCandidateBundle:
    """Build lagged factor candidates and market-cap/liquidity exposures.

    The decision row for session T uses only provider rows for the immediately
    preceding governed session. ``available_at`` is conservatively recorded as
    T's 15:00 Shanghai close. This is an explicit research assumption, not proof
    of historical provider vintages; see the false eligibility flags in manifest.
    """
    dependencies = TushareDependencyStore(source_root)
    sessions, all_calendar_bindings = _canonical_calendar(Path(bars_root))
    if len(sessions) < 2:
        raise ContractError("PIT candidate construction needs at least two governed sessions")
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ContractError("candidate window end precedes start")
    if start_date is not None and start_date not in sessions:
        raise ContractError("candidate start date is not in the governed calendar")
    if end_date is not None and end_date not in sessions:
        raise ContractError("candidate end date is not in the governed calendar")
    target_sessions = [
        (sessions[index - 1], sessions[index])
        for index in range(1, len(sessions))
        if (start_date is None or sessions[index] >= start_date)
        and (end_date is None or sessions[index] <= end_date)
    ]
    if not target_sessions:
        raise ContractError("candidate window contains no sessions with a prior source session")

    source_maps = {
        endpoint: _latest_daily_snapshot_map(dependencies, endpoint)
        for endpoint in ("daily_basic", "moneyflow")
    }
    required_source_days = {source for source, _ in target_sessions}
    for endpoint, snapshots in source_maps.items():
        missing = sorted(required_source_days - snapshots.keys())
        if missing:
            raise ContractError(
                f"{endpoint} lacks exact raw snapshots for required prior sessions: "
                f"{[day.isoformat() for day in missing[:5]]}"
            )

    factor_frames: list[pd.DataFrame] = []
    exposure_frames: list[pd.DataFrame] = []
    source_bindings: list[dict[str, Any]] = []
    for source_day, decision_day in target_sessions:
        snapshots = {endpoint: source_maps[endpoint][source_day] for endpoint in source_maps}
        basics = _read_raw_partition(
            dependencies, snapshots["daily_basic"], dataset="daily_basic", expected_day=source_day
        )
        flows = _read_raw_partition(
            dependencies, snapshots["moneyflow"], dataset="moneyflow", expected_day=source_day
        )
        required_basic = {
            "pe_ttm", "pb", "ps_ttm", "dv_ttm", "turnover_rate_f", "volume_ratio", "total_mv"
        }
        required_flow = {"net_mf_amount", "buy_lg_amount", "buy_elg_amount", "sell_lg_amount", "sell_elg_amount"}
        if required_basic - set(basics.columns) or required_flow - set(flows.columns):
            missing = sorted((required_basic - set(basics.columns)) | (required_flow - set(flows.columns)))
            raise ContractError(f"Tushare source snapshot lacks reviewed candidate fields: {missing}")

        basics["instrument"] = basics["ts_code"].map(_instrument)
        flows["instrument"] = flows["ts_code"].map(_instrument)
        if basics["instrument"].duplicated().any() or flows["instrument"].duplicated().any():
            raise ContractError(f"normalized instrument keys collide on {source_day.isoformat()}")
        for column in required_basic:
            basics[column] = _number_column(basics, column)
        for column in required_flow:
            flows[column] = _number_column(flows, column)
        if (basics["total_mv"].dropna() <= 0).any():
            raise ContractError(f"non-positive total_mv in daily_basic on {source_day.isoformat()}")
        if (basics["turnover_rate_f"].dropna() < 0).any():
            raise ContractError(f"negative turnover_rate_f on {source_day.isoformat()}")

        merged = basics[[
            "instrument", "pe_ttm", "pb", "ps_ttm", "dv_ttm", "turnover_rate_f",
            "volume_ratio", "total_mv",
        ]].merge(
            flows[["instrument", "net_mf_amount", "buy_lg_amount", "buy_elg_amount",
                   "sell_lg_amount", "sell_elg_amount"]],
            on="instrument", how="left", sort=False, validate="one_to_one",
        )
        cap = merged["total_mv"].where(merged["total_mv"] > 0)
        decision_date = decision_day.isoformat()
        factors = pd.DataFrame({
            "instrument": merged["instrument"],
            "decision_date": decision_date,
            "earnings_yield_ttm": (1.0 / merged["pe_ttm"].where(merged["pe_ttm"] > 0)),
            "book_to_price": (1.0 / merged["pb"].where(merged["pb"] > 0)),
            "sales_yield_ttm": (1.0 / merged["ps_ttm"].where(merged["ps_ttm"] > 0)),
            # Tushare daily_basic reports dividend yield in percent.
            "dividend_yield_ttm": merged["dv_ttm"] / 100.0,
            "turnover_rate_free_float": merged["turnover_rate_f"],
            "volume_ratio": merged["volume_ratio"],
            "moneyflow_net_to_market_cap": merged["net_mf_amount"] / cap,
            "large_order_net_to_market_cap": (
                merged["buy_lg_amount"] + merged["buy_elg_amount"]
                - merged["sell_lg_amount"] - merged["sell_elg_amount"]
            ) / cap,
        })
        factor_frames.append(factors)
        exposures = pd.DataFrame({
            "instrument": merged["instrument"],
            "datetime": decision_date,
            "market_cap": merged["total_mv"],
            "liquidity": merged["turnover_rate_f"],
            "available_at": datetime.combine(
                decision_day, time(15, 0), tzinfo=_SHANGHAI
            ).isoformat(),
        })
        exposure_frames.append(exposures)
        for endpoint, snapshot in snapshots.items():
            source_bindings.append({
                "dataset": endpoint,
                "trade_date": source_day.isoformat(),
                "generation_id": snapshot["generation_id"],
                "data_checksum_sha256": snapshot["data_artifact"]["checksum_sha256"],
                "source_fetched_at": snapshot["source_fetched_at"],
                "row_count": snapshot["row_count"],
            })

    factor_frame = pd.concat(factor_frames, ignore_index=True)
    exposure_frame = pd.concat(exposure_frames, ignore_index=True)
    factor_frame = factor_frame.sort_values(
        ["decision_date", "instrument"], kind="mergesort"
    ).reset_index(drop=True)
    exposure_frame = exposure_frame.sort_values(
        ["datetime", "instrument"], kind="mergesort"
    ).reset_index(drop=True)
    if factor_frame.duplicated(["instrument", "decision_date"]).any():
        raise ContractError("candidate factor panel contains duplicate instrument/session keys")
    if exposure_frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("candidate exposure panel contains duplicate instrument/session keys")
    exposure_frame = normalize_exposure_panel(
        exposure_frame,
        ["market_cap", "liquidity"],
    )
    for name in _FACTOR_COLUMNS:
        values = factor_frame[name].dropna().to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ContractError(f"candidate factor contains non-finite values: {name}")

    used_dates = {day.isoformat() for pair in target_sessions for day in pair}
    calendar_bindings = [
        item for item in all_calendar_bindings if item["session_date"] in used_dates
    ]
    factor_bytes = _parquet_bytes(factor_frame)
    exposure_bytes = _parquet_bytes(exposure_frame)
    environment = environment_profile()
    manifest: dict[str, Any] = {
        "contract_version": 1,
        "kind": "tushare_factor_candidate_bundle",
        "availability_policy": "prior_session_data_visible_by_decision_session_close_assumption_v1",
        "strict_pit_eligible": False,
        "historical_vintage_verified": False,
        "builder_code_sha256": file_sha256_bytes(Path(__file__).read_bytes()),
        "dependency_lock_sha256": environment["lockfile_sha256"],
        "factor_definitions": [dict(item) for item in _FACTOR_DEFINITIONS],
        "exposure_definitions": [dict(item) for item in _EXPOSURE_DEFINITIONS],
        "calendar_sessions": calendar_bindings,
        "source_snapshots": sorted(
            source_bindings,
            key=lambda item: (item["trade_date"], item["dataset"], item["generation_id"]),
        ),
        "factor_columns": list(_FACTOR_COLUMNS),
        "exposure_controls": ["market_cap", "liquidity"],
        "factor_artifact": {
            "filename": "factors.parquet",
            "checksum_sha256": file_sha256_bytes(factor_bytes),
            "row_count": len(factor_frame),
            "columns": factor_frame.columns.tolist(),
        },
        "exposure_artifact": {
            "filename": "exposures.parquet",
            "checksum_sha256": file_sha256_bytes(exposure_bytes),
            "row_count": len(exposure_frame),
            "columns": exposure_frame.columns.tolist(),
        },
    }
    manifest["generation_id"] = sha256_json(manifest)
    validate_contract("tushare_factor_candidate.v1.json", manifest)
    return TushareFactorCandidateBundle(factor_frame, exposure_frame, manifest)


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    import io

    sink = io.BytesIO()
    try:
        frame.to_parquet(sink, index=False, compression="snappy")
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise ContractError(f"candidate panel cannot be serialized as Parquet: {exc}") from exc
    return sink.getvalue()


class TushareFactorCandidateStore:
    """Immutable local store for diagnostic-only derived candidate bundles."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def publish(self, bundle: TushareFactorCandidateBundle) -> dict[str, Any]:
        manifest = dict(bundle.manifest)
        validate_contract("tushare_factor_candidate.v1.json", manifest)
        if sha256_json({key: value for key, value in manifest.items() if key != "generation_id"}) != manifest["generation_id"]:
            raise ContractError("candidate bundle generation identity mismatch")
        files = {
            "factors.parquet": _parquet_bytes(bundle.factors),
            "exposures.parquet": _parquet_bytes(bundle.exposures),
        }
        for filename, artifact in files.items():
            key = "factor_artifact" if filename == "factors.parquet" else "exposure_artifact"
            if file_sha256_bytes(artifact) != manifest[key]["checksum_sha256"]:
                raise ContractError(f"candidate {filename} bytes changed after construction")
        family = self.root / "tushare_factor_candidates"
        directory = family / f"generation={manifest['generation_id']}"
        lock_path = family / "publication.lock"
        reject_symlinks_within(self.root, family, label="Tushare candidate store path")
        family.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if directory.exists():
                existing = self.read(manifest["generation_id"])
                if existing["manifest"] != manifest:
                    raise ContractError("immutable candidate generation has conflicting metadata")
                return existing["manifest"]
            staging = family / f".staging_{uuid.uuid4().hex}"
            staging.mkdir()
            try:
                for filename, artifact in files.items():
                    (staging / filename).write_bytes(artifact)
                (staging / "manifest.json").write_text(
                    json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                fsync_tree(staging)
                os.replace(staging, directory)
                fsync_dir(family)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return manifest

    def read(
        self,
        generation_id: str,
        *,
        source_root: Path | str | None = None,
        bars_root: Path | str | None = None,
    ) -> dict[str, Any]:
        if len(generation_id) != 64 or any(char not in "0123456789abcdef" for char in generation_id):
            raise ContractError("invalid Tushare factor candidate generation id")
        directory = self.root / "tushare_factor_candidates" / f"generation={generation_id}"
        reject_symlinks_within(self.root, directory, label="Tushare candidate store path")
        if not directory.is_dir() or directory.is_symlink():
            raise ContractError("unpublished Tushare factor candidate bundle")
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("candidate manifest is missing or malformed") from exc
        validate_contract("tushare_factor_candidate.v1.json", manifest)
        if (
            manifest["generation_id"] != generation_id
            or sha256_json({key: value for key, value in manifest.items() if key != "generation_id"}) != generation_id
        ):
            raise ContractError("candidate manifest identity mismatch")
        for field in ("factor_artifact", "exposure_artifact"):
            artifact = manifest[field]
            path = directory / artifact["filename"]
            if not path.is_file() or path.is_symlink():
                raise ContractError("candidate panel artifact is unavailable")
            payload = path.read_bytes()
            if file_sha256_bytes(payload) != artifact["checksum_sha256"]:
                raise ContractError("candidate panel artifact checksum mismatch")
            frame = pd.read_parquet(path)
            if len(frame) != artifact["row_count"] or frame.columns.tolist() != artifact["columns"]:
                raise ContractError("candidate panel artifact shape mismatch")
        if (source_root is None) != (bars_root is None):
            raise ContractError("candidate lineage verification requires both source_root and bars_root")
        if source_root is not None and bars_root is not None:
            self._verify_lineage(manifest, source_root=source_root, bars_root=bars_root)
        return {"manifest": manifest, "path": directory}

    @staticmethod
    def _verify_lineage(
        manifest: dict[str, Any], *, source_root: Path | str, bars_root: Path | str
    ) -> None:
        dependency_store = TushareDependencyStore(source_root)
        for binding in manifest["source_snapshots"]:
            upstream, frame = dependency_store.read_frame(
                binding["generation_id"], dataset=binding["dataset"]
            )
            if (
                upstream["data_artifact"]["checksum_sha256"] != binding["data_checksum_sha256"]
                or upstream["source_fetched_at"] != binding["source_fetched_at"]
                or upstream["row_count"] != binding["row_count"]
                or _query_date(upstream).isoformat() != binding["trade_date"]
                or frame.empty
            ):
                raise ContractError("candidate raw-source lineage no longer matches its owning snapshot")
        _, calendar_bindings = _canonical_calendar(Path(bars_root))
        date_scope = {entry["session_date"] for entry in manifest["calendar_sessions"]}
        expected = [item for item in calendar_bindings if item["session_date"] in date_scope]
        if expected != manifest["calendar_sessions"]:
            raise ContractError("candidate governed-calendar lineage no longer matches canonical bars")
