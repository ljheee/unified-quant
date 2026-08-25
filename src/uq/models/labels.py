from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError
from ..factors.raw_price import logical_fingerprint

_LABEL_HORIZON = 5


def _validate_adjusted_price_frame(frame: pd.DataFrame) -> None:
    required = {"instrument", "datetime", "close", "adj_factor"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ContractError(f"adjusted price input missing columns: {missing}")
    if frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("duplicate instrument/date keys in adjusted price input")
    if not (frame["adj_factor"] > 0).all():
        raise ContractError("non-positive adjustment factor in adjusted price input")
    if frame["close"].isna().any() or frame["adj_factor"].isna().any():
        raise ContractError("null close/adj_factor in adjusted price input")


def _formula_sha256(horizon: int) -> str:
    formula = f"adjusted_close[D+{horizon}] / adjusted_close[D] - 1"
    return hashlib.sha256(formula.encode("utf-8")).hexdigest()


class LabelBuilder:
    """Build a 5-trading-day adjusted-close label set from governed inputs."""

    def __init__(
        self,
        *,
        name: str,
        semantic_version: str,
        horizon: int = _LABEL_HORIZON,
        adjustment_basis: str = "governed_adjusted_close",
        code_fingerprint: str | None = None,
    ) -> None:
        if horizon < 1:
            raise ContractError("label horizon must be positive")
        self.name = name
        self.semantic_version = semantic_version
        self.horizon = horizon
        self.adjustment_basis = adjustment_basis
        self.code_fingerprint = code_fingerprint or sha256_json({"component": "LabelBuilder", "version": 1})

    def build(
        self,
        frame: pd.DataFrame,
        *,
        upstream_bindings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compute labels and produce a manifest without identity digests."""
        _validate_adjusted_price_frame(frame)
        for binding in upstream_bindings:
            if binding.get("binding") != "adjusted_price":
                raise ContractError("label builder only accepts adjusted_price bindings")
            if len(binding.get("generation_id", "")) != 64:
                raise ContractError("invalid upstream generation_id in binding")

        ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
        adjusted_close = ordered["close"].astype(float) * ordered["adj_factor"].astype(float)
        ordered["decision_date"] = ordered["datetime"]
        ordered["label"] = (
            adjusted_close.groupby(ordered["instrument"], sort=False)
            .transform(lambda values: values.shift(-self.horizon) / values - 1)
        )
        # Rows without complete future observations remain null.
        output = ordered[["instrument", "decision_date", "label"]].copy()
        null_count = int(output["label"].isna().sum())
        terminal_null_count = null_count  # first slice: no terminal-return policy

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "name": self.name,
            "semantic_version": self.semantic_version,
            "primary_key": ["instrument", "decision_date"],
            "decision_time_convention": "trading_close_asia_shanghai",
            "horizon_trading_days": self.horizon,
            "formula_sha256": _formula_sha256(self.horizon),
            "adjustment_basis": self.adjustment_basis,
            "benchmark_binding": None,
            "upstream_adjusted_price_bindings": upstream_bindings,
            "eligibility": {"rules": {"suspension": "exclude", "listing_age_minimum_days": 60}},
            "terminal_return_policy": None,
            "null_policy": {"insufficient_future": "null"},
            "row_count": len(output),
            "columns": list(output.columns),
            "dtypes": {column: str(dtype) for column, dtype in output.dtypes.items()},
            "data_checksum_sha256": sha256_json({"rows": [
                [str(row[0]), pd.Timestamp(row[1]).isoformat(), None if pd.isna(row[2]) else float(row[2])]
                for row in output.itertuples(index=False)
            ]}),
            "logical_fingerprint": logical_fingerprint(output.rename(columns={"decision_date": "datetime"})),
            "code_fingerprint": self.code_fingerprint,
            "serialization_profile_id": "parquet-v1",
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest["generation_id"] = "0" * 64
        manifest["manifest_digest_sha256"] = "0" * 64
        generation_id, manifest_digest = model_manifest_identities(manifest, schema_name="label_set")
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("label_set", manifest)
        return manifest


class LabelValidator:
    @staticmethod
    def validate_manifest(manifest: Mapping[str, Any]) -> None:
        ModelContractLoader.validate("label_set", dict(manifest))
        if manifest.get("horizon_trading_days") != _LABEL_HORIZON:
            raise ContractError("unsupported label horizon for first release")
        if manifest.get("benchmark_binding") is not None:
            raise ContractError("benchmark labels are excluded from the first release")
