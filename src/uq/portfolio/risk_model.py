"""Governed daily returns and deterministic risk-budget covariance helpers."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from ..contracts.canonical_v2 import fsync_dir, fsync_tree
from ..contracts.gate_contracts import canonical_json, sha256_json, validate_contract
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError

RETURN_COLUMNS = ["date", "instrument", "return"]
_INSTRUMENT_PATTERN = re.compile(r"[0-9]{6}\.(XSHG|XSHE|XBSE)")


def normalize_daily_returns(
    frame: pd.DataFrame, calendar_sessions: Sequence[str]
) -> pd.DataFrame:
    sessions = _normalize_sessions(calendar_sessions)
    if not isinstance(frame, pd.DataFrame) or frame.empty or set(frame.columns) != set(RETURN_COLUMNS):
        raise ContractError("portfolio risk returns must be nonempty with exactly date/instrument/return")
    result = frame[RETURN_COLUMNS].copy()
    for value in result["date"]:
        if not isinstance(value, str):
            raise ContractError("portfolio risk returns require ISO session dates")
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ContractError("portfolio risk returns require ISO session dates") from exc
    if not set(result["date"]) <= set(sessions):
        raise ContractError("portfolio risk returns contain dates outside the bound calendar")
    if not result["instrument"].map(
        lambda value: isinstance(value, str) and _INSTRUMENT_PATTERN.fullmatch(value) is not None
    ).all():
        raise ContractError("portfolio risk returns require canonical instrument identifiers")
    if result.duplicated(["date", "instrument"]).any():
        raise ContractError("portfolio risk returns contain duplicate date/instrument keys")
    values = result["return"]
    if not pd.api.types.is_numeric_dtype(values) or pd.api.types.is_bool_dtype(values):
        raise ContractError("portfolio risk returns must be numeric decimal simple returns")
    numeric = values.to_numpy(dtype=float)
    if not np.isfinite(numeric).all() or (numeric <= -1).any():
        raise ContractError("portfolio risk returns must be finite and greater than -100%")
    result["return"] = numeric
    return result.sort_values(["date", "instrument"], kind="mergesort").reset_index(drop=True)


def daily_returns_fingerprint(frame: pd.DataFrame) -> str:
    subset = frame[RETURN_COLUMNS].astype(object)
    rows = subset.where(pd.notna(subset), None)
    return sha256_json({
        "profile": "portfolio_daily_returns_v1",
        "columns": RETURN_COLUMNS,
        "rows": rows.to_dict(orient="records"),
    })


def covariance_for_decision(
    frame: pd.DataFrame,
    *,
    calendar_sessions: Sequence[str],
    decision_date: str,
    instruments: Sequence[str],
    lookback_sessions: int,
) -> pd.DataFrame:
    sessions = _normalize_sessions(calendar_sessions)
    if not isinstance(frame, pd.DataFrame) or set(frame.columns) != set(RETURN_COLUMNS):
        raise ContractError("risk parity requires normalized date/instrument/return rows")
    if frame.duplicated(["date", "instrument"]).any():
        raise ContractError("risk parity returns contain duplicate date/instrument keys")
    if type(lookback_sessions) is not int or lookback_sessions < 2:
        raise ContractError("risk parity lookback must contain at least two sessions")
    if not isinstance(decision_date, str) or decision_date not in sessions:
        raise ContractError("risk parity decision date is outside the bound return calendar")
    if not instruments or len(instruments) != len(set(instruments)):
        raise ContractError("risk parity covariance requires unique selected instruments")
    index = sessions.index(decision_date)
    if index < lookback_sessions:
        raise ContractError("risk parity has insufficient strictly prior return sessions")
    sample_sessions = sessions[index - lookback_sessions:index]
    sample = frame.loc[
        frame["date"].isin(sample_sessions) & frame["instrument"].isin(instruments),
        ["date", "instrument", "return"],
    ]
    matrix = sample.pivot(index="date", columns="instrument", values="return")
    matrix = matrix.reindex(index=sample_sessions, columns=list(instruments))
    if matrix.isna().any().any():
        missing = int(matrix.isna().sum().sum())
        raise ContractError(
            f"risk parity returns are incomplete for the selected basket ({missing} missing observations)"
        )
    observations = matrix.to_numpy(dtype=float)
    if not np.isfinite(observations).all():
        raise ContractError("risk parity returns contain non-finite observations")
    covariance = np.atleast_2d(np.cov(observations, rowvar=False, ddof=1))
    if covariance.shape != (len(instruments), len(instruments)):
        raise ContractError("risk parity covariance has an invalid shape")
    if not np.isfinite(covariance).all():
        raise ContractError("risk parity covariance contains non-finite values")
    return pd.DataFrame(covariance, index=list(instruments), columns=list(instruments))


def risk_parity_weights(
    covariance: pd.DataFrame | np.ndarray,
    *,
    tolerance: float = 1e-10,
    max_iterations: int = 20_000,
) -> np.ndarray:
    """Solve equal risk contributions for a positive-definite covariance matrix.

    This solves the long-only risk-budgeting equations using cyclic coordinate
    updates. Input order is preserved. Constraints applied by the caller can
    subsequently leave the final constrained portfolio with unequal risk.
    """
    if type(max_iterations) is not int or max_iterations < 1:
        raise ContractError("risk parity iteration limit must be a positive integer")
    if (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float))
            or not math.isfinite(float(tolerance)) or tolerance <= 0):
        raise ContractError("risk parity tolerance must be finite and positive")
    try:
        values = (
            covariance.to_numpy(dtype=float)
            if isinstance(covariance, pd.DataFrame)
            else np.asarray(covariance, dtype=float)
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("risk parity covariance must be a numeric matrix") from exc
    if values.ndim != 2 or values.shape[0] != values.shape[1] or values.shape[0] < 1:
        raise ContractError("risk parity covariance must be a nonempty square matrix")
    if not np.isfinite(values).all():
        raise ContractError("risk parity covariance must be finite")
    scale = max(float(np.max(np.abs(values))), 1.0)
    if not np.allclose(values, values.T, rtol=0.0, atol=1e-12 * scale):
        raise ContractError("risk parity covariance must be symmetric")
    values = (values + values.T) / 2.0
    diagonal = np.diag(values)
    if (diagonal <= 0).any():
        raise ContractError("risk parity covariance must have positive variances")
    try:
        np.linalg.cholesky(values)
    except np.linalg.LinAlgError as exc:
        raise ContractError("risk parity covariance must be positive definite") from exc

    count = values.shape[0]
    budget = np.full(count, 1.0 / count)
    x = 1.0 / np.sqrt(diagonal)
    for _ in range(max_iterations):
        previous = x.copy()
        for index in range(count):
            cross = float(values[index] @ x - diagonal[index] * x[index])
            root = math.sqrt(cross * cross + 4.0 * diagonal[index] * budget[index])
            denominator = cross + root
            x[index] = (
                2.0 * budget[index] / denominator
                if denominator > 1e-300
                else (-cross + root) / (2.0 * diagonal[index])
            )
            if not math.isfinite(x[index]) or x[index] <= 0:
                raise ContractError("risk parity solver produced an invalid weight")
        weights = x / x.sum()
        contributions = weights * (values @ weights)
        total = float(contributions.sum())
        if not math.isfinite(total) or total <= 0:
            raise ContractError("risk parity portfolio variance is not positive")
        normalized_contributions = contributions / total
        if float(np.max(np.abs(normalized_contributions - budget))) <= tolerance:
            return weights
        if float(np.max(np.abs(x - previous) / np.maximum(np.abs(previous), 1e-15))) <= tolerance:
            break
    raise ContractError("risk parity solver did not converge to equal risk contributions")


class PortfolioRiskReturnsStore:
    """Immutable source-declared daily returns consumed by risk-parity weights."""

    def __init__(self, root: Path | str, *, calendar_store=None) -> None:
        self.root = Path(root)
        self.calendar_store = calendar_store

    def directory_for(self, generation_id: str) -> Path:
        if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{64}", generation_id):
            raise ContractError("invalid portfolio risk return generation")
        path = self.root / "portfolio_risk_returns" / f"generation={generation_id}"
        reject_symlinks_within(self.root, path, label="portfolio risk returns")
        return path

    def publish(
        self,
        frame: pd.DataFrame,
        *,
        calendar_sessions: Sequence[str],
        calendar_generation_id: str,
        calendar_manifest_digest_sha256: str,
        calendar_data_checksum_sha256: str,
        source_id: str,
        source_approval_ref: str,
    ) -> dict:
        if not all(isinstance(value, str) and value.strip() for value in (source_id, source_approval_ref)):
            raise ContractError("portfolio risk returns require source identity and approval reference")
        for value in (calendar_generation_id, calendar_manifest_digest_sha256, calendar_data_checksum_sha256):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ContractError("portfolio risk returns require bound calendar identities")
        sessions = _normalize_sessions(calendar_sessions)
        self._verify_calendar_binding(
            calendar_generation_id,
            calendar_manifest_digest_sha256,
            calendar_data_checksum_sha256,
            sessions,
        )
        normalized = normalize_daily_returns(frame, sessions)
        base = self.root / "portfolio_risk_returns"
        reject_symlinks_within(self.root, base, label="portfolio risk returns")
        base.mkdir(parents=True, exist_ok=True)
        lock_path = base / "publication.lock"
        reject_symlinks_within(self.root, lock_path, label="portfolio risk returns lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            staging = base / f".staging-{uuid.uuid4().hex}"
            try:
                staging.mkdir()
                normalized.to_parquet(staging / "data.parquet", index=False)
                payload = {
                    "contract_version": 1,
                    "source_id": source_id,
                    "source_approval_ref": source_approval_ref,
                    "return_method": "adjusted_close_simple_return_decimal",
                    "observation_visibility": "available_after_session_close",
                    "strict_prior_session_policy": True,
                    "calendar_generation_id": calendar_generation_id,
                    "calendar_manifest_digest_sha256": calendar_manifest_digest_sha256,
                    "calendar_data_checksum_sha256": calendar_data_checksum_sha256,
                    "calendar_sessions": list(sessions),
                    "window_start": sessions[0],
                    "window_end": sessions[-1],
                    "row_count": len(normalized),
                    "columns": RETURN_COLUMNS,
                    "data_checksum_sha256": hashlib.sha256((staging / "data.parquet").read_bytes()).hexdigest(),
                    "logical_checksum_sha256": daily_returns_fingerprint(normalized),
                }
                generation = sha256_json(payload)
                manifest = {**payload, "generation_id": generation}
                manifest["manifest_digest_sha256"] = sha256_json(manifest)
                validate_contract("portfolio_risk_returns.v1.json", manifest)
                (staging / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
                target = self.directory_for(generation)
                if target.exists():
                    existing, _ = self.read(generation)
                    if existing != manifest:
                        raise ContractError("portfolio risk returns identity conflict")
                    return existing
                self._read(staging, generation)
                fsync_tree(staging)
                os.rename(staging, target)
                fsync_dir(base)
                return manifest
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def read(self, generation_id: str) -> tuple[dict, pd.DataFrame]:
        return self._read(self.directory_for(generation_id), generation_id)

    def _read(self, directory: Path, generation_id: str) -> tuple[dict, pd.DataFrame]:
        for name in ("manifest.json", "data.parquet"):
            reject_symlinks_within(self.root, directory / name, label="portfolio risk returns")
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError("portfolio risk returns unavailable or malformed") from exc
        validate_contract("portfolio_risk_returns.v1.json", manifest)
        payload = {key: value for key, value in manifest.items()
                   if key not in {"generation_id", "manifest_digest_sha256"}}
        if (manifest["generation_id"] != generation_id
                or sha256_json(payload) != generation_id
                or sha256_json({**payload, "generation_id": generation_id})
                != manifest["manifest_digest_sha256"]):
            raise ContractError("portfolio risk returns manifest identity mismatch")
        self._verify_calendar_binding(
            manifest["calendar_generation_id"],
            manifest["calendar_manifest_digest_sha256"],
            manifest["calendar_data_checksum_sha256"],
            _normalize_sessions(manifest["calendar_sessions"]),
        )
        try:
            checksum = hashlib.sha256((directory / "data.parquet").read_bytes()).hexdigest()
            frame = pd.read_parquet(directory / "data.parquet")
        except OSError as exc:
            raise ContractError("portfolio risk returns payload unavailable") from exc
        if checksum != manifest["data_checksum_sha256"]:
            raise ContractError("portfolio risk returns data checksum mismatch")
        normalized = normalize_daily_returns(frame, manifest["calendar_sessions"])
        if (len(normalized) != manifest["row_count"]
                or daily_returns_fingerprint(normalized) != manifest["logical_checksum_sha256"]
                or manifest["window_start"] != manifest["calendar_sessions"][0]
                or manifest["window_end"] != manifest["calendar_sessions"][-1]):
            raise ContractError("portfolio risk returns logical content mismatch")
        return manifest, normalized

    def _verify_calendar_binding(
        self,
        generation_id: str,
        manifest_digest_sha256: str,
        data_checksum_sha256: str,
        calendar_sessions: Sequence[str],
    ) -> None:
        if self.calendar_store is None:
            return
        calendar_manifest = self.calendar_store.read_manifest_by(
            generation_id, dataset_type="calendar"
        )
        calendar_payload = {
            key: value for key, value in calendar_manifest.items()
            if key != "manifest_digest_sha256"
        }
        if (calendar_manifest.get("generation_id") != generation_id
                or sha256_json(calendar_payload) != manifest_digest_sha256
                or calendar_manifest.get("manifest_digest_sha256") != manifest_digest_sha256
                or calendar_manifest.get("data_checksum_sha256") != data_checksum_sha256):
            raise ContractError("portfolio risk returns calendar binding mismatch")
        calendar_frame = self.calendar_store.read_frame(
            generation_id,
            data_checksum_sha256,
            dataset_type="calendar",
            required_columns={"date"},
        )
        actual_sessions = _normalize_sessions(
            sorted(set(calendar_frame["date"].astype(str).tolist()))
        )
        if actual_sessions != tuple(calendar_sessions):
            raise ContractError("portfolio risk returns calendar sessions mismatch")


def _normalize_sessions(values: Sequence[str]) -> tuple[str, ...]:
    sessions = tuple(values)
    if not sessions or any(not isinstance(value, str) for value in sessions):
        raise ContractError("portfolio risk returns require a nonempty trading calendar")
    for value in sessions:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ContractError("portfolio risk return calendar requires ISO dates") from exc
    if tuple(sorted(set(sessions))) != sessions:
        raise ContractError("portfolio risk return calendar must be sorted and unique")
    return sessions
