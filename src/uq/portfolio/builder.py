"""Portfolio layer: convert prediction scores into governed target weights."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as arrow
import pyarrow.parquet as parquet

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    model_manifest_identities,
    sha256_json,
    validate_quality_decision_owning_report,
)
from ..errors import ContractError

_WEIGHT_TOLERANCE = 1e-8


class PortfolioBuilder:
    """Build target weights from a prediction frame and portfolio definition."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.portfolio_dir = self.root / "portfolios"

    def build(
        self,
        *,
        definition: dict[str, Any],
        prediction_generation_id: str,
        decision_date: str,
        scores: pd.Series,
        universe_instruments: list[str],
        previous_target_weights: dict[str, float] | None = None,
        quality_decision: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], pd.DataFrame]:
        """Compute target weights for one rebalance date.

        Args:
            definition: validated portfolio_definition.v1 manifest.
            prediction_generation_id: upstream prediction generation.
            decision_date: ISO date string.
            scores: instrument-indexed score series (already filtered to eligible).
            universe_instruments: allowed instruments from the universe snapshot.
            previous_target_weights: prior date's {instrument: weight} mapping.
                Required when max_turnover constraint is active.
            quality_decision: externally reviewed quality decision document.

        Returns:
            (manifest, frame) where frame has columns [instrument, weight].
        """
        if definition.get("weight_scheme") != "top_n_equal_weight":
            raise ContractError(f"unsupported weight scheme: {definition.get('weight_scheme')}")

        scheme_params = definition["scheme_parameters"]
        n = scheme_params["n"]
        constraints = definition["constraints"]
        max_single = constraints["max_single_weight"]
        max_industry = constraints.get("max_industry_weight")
        max_turnover = constraints.get("max_turnover")
        cash_reserve = constraints.get("cash_reserve", 0.0)

        # Score policy: descending, exclude NaN, tie-break by instrument ID
        policy = definition["score_policy"]
        if policy["direction"] != "descending":
            raise ContractError("only descending direction supported")
        if policy["nan_policy"] != "exclude":
            raise ContractError("only exclude nan policy supported")

        valid_scores = scores.dropna()
        valid_scores = valid_scores[valid_scores.index.isin(universe_instruments)]
        if len(valid_scores) < n:
            raise ContractError(
                f"insufficient eligible instruments ({len(valid_scores)}) for top-{n} portfolio"
            )

        # Select top N by score desc, tie-break by instrument ID ascending
        ranked = valid_scores.reset_index()
        ranked.columns = ["instrument", "score"]
        ranked = ranked.sort_values(["score", "instrument"], ascending=[False, True])
        selected = ranked.head(n)["instrument"].tolist()

        # Equal weight with single-position cap
        raw_weight = 1.0 / n
        capped_weight = min(raw_weight, max_single)
        total_stock = capped_weight * n

        # Cash reserve: scale down if needed
        max_allowed_total = 1.0 - cash_reserve + _WEIGHT_TOLERANCE
        if total_stock > max_allowed_total:
            scale_factor = max_allowed_total / total_stock
            capped_weight *= scale_factor
            total_stock = capped_weight * n

        weights_map = {inst: capped_weight for inst in selected}

        # Industry cap: requires industry_source_binding and per-instrument industry mapping
        if max_industry is not None and max_industry < 1.0:
            industry_binding = definition.get("industry_source_binding")
            if not industry_binding or industry_binding.get("source_type") != "governed_industry_manifest":
                raise ContractError(
                    "max_industry_weight requires a governed_industry_manifest binding"
                )
            if not isinstance(definition.get("_industry_mapping"), dict):
                raise ContractError(
                    "industry mapping must be provided as _industry_mapping {instrument: industry_id}"
                )
            industry_mapping: dict[str, str] = definition["_industry_mapping"]
            # Compute industry totals
            industry_totals: dict[str, float] = {}
            for inst, w in weights_map.items():
                ind = industry_mapping.get(inst)
                if ind is None:
                    continue
                industry_totals[ind] = industry_totals.get(ind, 0.0) + w
            # Scale down over-cap industries proportionally (residual to cash)
            for ind, total in sorted(industry_totals.items()):
                if total > max_industry + _WEIGHT_TOLERANCE:
                    scale = max_industry / total
                    for inst in list(weights_map.keys()):
                        if industry_mapping.get(inst) == ind:
                            weights_map[inst] *= scale
                    residual_reduction = total - total * scale
                    total_stock -= residual_reduction

        # Turnover cap
        if max_turnover is not None:
            if max_turnover > 0 and previous_target_weights is None:
                raise ContractError(
                    "previous_target_weights is required when max_turnover is set"
                )
            if previous_target_weights is not None:
                all_keys = sorted(set(weights_map.keys()) | set(previous_target_weights.keys()))
                turnover = 0.5 * sum(
                    abs(weights_map.get(k, 0.0) - previous_target_weights.get(k, 0.0))
                    for k in all_keys
                )
                if turnover > max_turnover + _WEIGHT_TOLERANCE:
                    alpha = max_turnover / turnover
                    blended = {}
                    for k in all_keys:
                        prev_w = previous_target_weights.get(k, 0.0)
                        target_w = weights_map.get(k, 0.0)
                        new_w = prev_w + alpha * (target_w - prev_w)
                        if new_w > _WEIGHT_TOLERANCE:
                            blended[k] = round(new_w, 12)
                    weights_map = blended
                    total_stock = sum(weights_map.values())

        frame = pd.DataFrame(
            [(k, v) for k, v in sorted(weights_map.items())],
            columns=["instrument", "weight"],
        )

        manifest = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "decision_date": decision_date,
            "portfolio_definition_generation_id": definition["generation_id"],
            "prediction_set_generation_id": prediction_generation_id,
            "universe_snapshot_generation_id": definition["universe_snapshot_generation_id"],
            "instrument_count": len(frame),
            "total_stock_weight": round(total_stock, 12),
            "cash_reserve": cash_reserve,
            "previous_target_weights_generation_id": None,  # set by caller/store
            "weights_file": "data.parquet",
            "weights_checksum_sha256": "0" * 64,  # placeholder; set at publish time
            "columns": ["instrument", "weight"],
            "dtypes": {"instrument": "string", "weight": "float64"},
            "row_count": len(frame),
            "key_uniqueness": "instrument",
            "logical_fingerprint": "0" * 64,  # computed at publish
            "serialization_profile_id": "parquet-v1",
            "run_id": "00000000-0000-4000-8000-000000000000",
            "created_at": "1970-01-01T00:00:00+00:00",
            "quality_report_checksum_sha256": "0" * 64,
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        return manifest, frame



class PortfolioDefinitionBinding:
    """Bind a reviewed portfolio definition to its immutable owning quality report."""

    @staticmethod
    def bind(
        definition: dict[str, Any],
        *,
        quality_decision: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ModelContractLoader.validate("portfolio_definition", definition)
        report = quality_decision.get("owning_report")
        if not isinstance(report, dict):
            raise ContractError("portfolio quality decision missing owning report")
        checksum = validate_quality_decision_owning_report(report)
        if quality_decision.get("binding_type") != "portfolio_definition_v1":
            raise ContractError("portfolio quality decision binding mismatch")
        if checksum != quality_decision.get("decision_checksum_sha256"):
            raise ContractError("portfolio quality decision checksum mismatch")
        if report.get("bound_generation_id") != definition["generation_id"]:
            raise ContractError("portfolio quality decision is bound to another definition")
        if quality_decision.get("trust_anchor_id") != report.get("key_id"):
            raise ContractError("portfolio quality decision trust anchor mismatch")
        return definition, report


class TargetWeightStore:
    """Publish and read immutable target-weight partitions."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.weights_dir = self.root / "target_weights"
        self.reviews_dir = self.root / "external_quality_reviews"

    def publish(
        self,
        manifest: dict[str, Any],
        frame: pd.DataFrame,
        *,
        quality_decision: dict[str, Any],
        previous_target_weights_generation_id: str | None = None,
    ) -> Path:
        """Publish a target-weights partition atomically."""
        artifact, checksum = self._serialize(frame)

        manifest["weights_checksum_sha256"] = checksum
        manifest["previous_target_weights_generation_id"] = previous_target_weights_generation_id

        # Step 1: compute logical fingerprint (now participates in generation)
        manifest["logical_fingerprint"] = sha256_json({
            "instruments": sorted(frame["instrument"].tolist()),
            "weights": [round(w, 12) for w in frame.sort_values("instrument")["weight"].tolist()],
        })

        # Step 2: compute provisional generation WITHOUT quality checksum for report binding
        provisional_gen, _ = model_manifest_identities(
            {**manifest, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="target_weights",
        )

        # Step 3: bind quality decision to provisional generation + content checksum
        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="target_weights_v1",
            subject_generation_id=provisional_gen,
            subject_content_sha256=checksum,
        )
        manifest["quality_report_checksum_sha256"] = report_checksum

        # Step 4: compute final identity WITH quality checksum participating
        final_gen, digest = model_manifest_identities(
            {**manifest, "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="target_weights",
        )
        manifest["generation_id"] = final_gen
        manifest["manifest_digest_sha256"] = digest

        # Step 5: validate complete manifest including identity checks
        ModelContractLoader.validate("target_weights", manifest)

        partition = (
            self.weights_dir
            / f"date={manifest['decision_date']}"
            / f"generation={final_gen}"
        )
        data_path = partition / "data.parquet"
        manifest_path = partition / "manifest.json"
        review_path = self.reviews_dir / f"{report_checksum}.json"
        if data_path.exists() or manifest_path.exists():
            raise ContractError(f"target-weight partition already exists: {partition}")

        staging = partition.parent / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            artifact, _ = self._serialize(frame)
            (staging / "data.parquet").write_bytes(artifact)
            self._atomic_write_json(staging / "manifest.json", manifest)
            self.reviews_dir.mkdir(parents=True, exist_ok=True)
            if not review_path.exists():
                review_tmp = review_path.with_suffix(".tmp")
                review_tmp.write_text(json.dumps(bound_report, sort_keys=True, indent=2) + "\n")
                os.replace(review_tmp, review_path)
                fsync_dir(self.reviews_dir)
            fsync_tree(staging)
            os.replace(staging, partition)
            fsync_dir(partition.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return partition

    def read(self, generation_id: str, decision_date: str) -> tuple[dict[str, Any], pd.DataFrame]:
        """Read a target-weights partition with fail-closed verification."""
        candidates = list(
            (self.weights_dir / f"date={decision_date}").glob(f"generation={generation_id}")
        ) if (self.weights_dir / f"date={decision_date}").exists() else []
        if not candidates:
            raise ContractError(f"unpublished target weights: generation={generation_id} date={decision_date}")
        partition = candidates[0]
        manifest_path = partition / "manifest.json"
        data_path = partition / "data.parquet"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"incomplete target-weight partition: {partition}")

        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed target-weight manifest") from exc

        ModelContractLoader.validate("target_weights", manifest)
        if manifest["generation_id"] != generation_id:
            raise ContractError("target-weight generation mismatch on read")

        checksum = manifest["quality_report_checksum_sha256"]
        review_path = self.reviews_dir / f"{checksum}.json"
        if not review_path.is_file():
            raise ContractError("target-weight quality report unavailable")
        try:
            report = json.loads(review_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed target-weight quality report") from exc
        ModelContractLoader.validate("model_quality_report", report)
        if report.get("binding_type") != "target_weights_v1":
            raise ContractError("quality report binding type mismatch")
        # Quality report binds to the provisional generation (computed with quality=zero).
        # Recompute it from the published manifest for verification.
        prov_gen, _ = model_manifest_identities(
            {**manifest, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="target_weights",
        )
        if report.get("bound_generation_id") != prov_gen:
            raise ContractError("quality report does not bind this generation")
        if report.get("status") not in ("passed", "warning"):
            raise ContractError("quality report rejects read")

        actual_checksum = file_sha256_bytes(data_path.read_bytes())
        if actual_checksum != manifest["weights_checksum_sha256"]:
            raise ContractError("tampered target-weight data prevents read")

        if manifest.get("serialization_profile_id") != "parquet-v1":
            raise ContractError("unsupported target-weights serialization profile")
        frame = pd.read_parquet(data_path)
        if list(frame.columns) != manifest.get("columns"):
            raise ContractError("target-weight column mismatch on read")
        if len(frame) != manifest.get("row_count"):
            raise ContractError("target-weight row count mismatch on read")
        for col_name, expected_dtype in manifest.get("dtypes", {}).items():
            if col_name in frame.columns:
                actual_dtype = str(frame[col_name].dtype)
                if "float" in expected_dtype and "float" not in actual_dtype:
                    raise ContractError(f"dtype mismatch for {col_name}: expected {expected_dtype}, got {actual_dtype}")
                elif expected_dtype == "string" and "object" not in actual_dtype and "str" not in actual_dtype:
                    raise ContractError(f"dtype mismatch for {col_name}: expected string, got {actual_dtype}")
        if frame.duplicated(subset=["instrument"]).any():
            raise ContractError("duplicate instruments in target-weight data")
        if frame["weight"].isna().any() or not np.isfinite(frame["weight"]).all():
            raise ContractError("invalid weights detected on read")
        if (frame["weight"] < -_WEIGHT_TOLERANCE).any():
            raise ContractError("negative weight detected on read")
        return manifest, frame

    @staticmethod
    def _serialize(frame: pd.DataFrame) -> tuple[bytes, str]:
        ordered = frame.sort_values("instrument", kind="mergesort").reset_index(drop=True)
        table = arrow.Table.from_pandas(ordered, preserve_index=False)
        sink = arrow.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        os.replace(tmp, path)
