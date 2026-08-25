from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError


class ModelDefinitionBuilder:
    """Build a reviewed model definition manifest."""

    def __init__(
        self,
        *,
        run_content_generation_id: str,
        reviewed: bool = False,
        code_fingerprint: str | None = None,
    ) -> None:
        self.code_fingerprint = code_fingerprint or sha256_json({"component": "ModelDefinitionBuilder", "version": 1})
        if not isinstance(run_content_generation_id, str) or len(run_content_generation_id) != 64:
            raise ContractError("model definition requires a valid model_run_content_generation_id")
        if not reviewed:
            raise ContractError("model definitions must be explicitly marked reviewed by an external reviewer")
        self.run_content_generation_id = run_content_generation_id

    def build(
        self,
        *,
        model_set: str,
        model_version: str,
        algorithm: str,
        hyperparameters: dict[str, Any],
        seed_policy: dict[str, Any],
        feature_schema_generation_id: str,
        compatible_dataset_versions: list[str],
        metrics: list[dict[str, Any]],
        selection_rule: str,
        quality_policy: str = "reject_all",
        serializer_version: str = "joblib-v1",
    ) -> dict[str, Any]:
        if algorithm not in ("regularized_linear", "lightgbm"):
            raise ContractError(f"unsupported algorithm: {algorithm}")
        if not metrics:
            raise ContractError("model definition requires at least one metric")
        for metric in metrics:
            if metric["direction"] not in ("maximize", "minimize"):
                raise ContractError(f"invalid metric direction: {metric['direction']}")
        if quality_policy not in ("reject_all", "accept_with_warnings"):
            raise ContractError(f"invalid quality policy: {quality_policy}")

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "model_set": model_set,
            "model_version": model_version,
            "status": "reviewed",
            "algorithm": algorithm,
            "hyperparameters": hyperparameters,
            "seed_policy": seed_policy,
            "feature_schema_generation_id": feature_schema_generation_id,
            "compatible_dataset_versions": compatible_dataset_versions,
            "metrics": metrics,
            "selection_rule": selection_rule,
            "quality_policy": quality_policy,
            "serializer_version": serializer_version,
            "model_run_content_generation_id": self.run_content_generation_id,
            "code_fingerprint": self.code_fingerprint,
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id, manifest_digest = model_manifest_identities(manifest, schema_name="model_definition")
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("model_definition", manifest)
        return manifest


class MetricReport:
    """Compute and serialize metric values from predictions vs actuals."""

    @staticmethod
    def compute(
        predictions: pd.DataFrame,
        actuals: pd.DataFrame,
        *,
        metric_definitions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        key_columns = {"instrument", "decision_date"}
        pred_value_cols = [c for c in predictions.columns if c not in key_columns]
        actual_value_cols = [c for c in actuals.columns if c not in key_columns]
        if len(pred_value_cols) != 1 or len(actual_value_cols) != 1:
            raise ContractError("predictions/actuals must have exactly one value column each")
        score_col = pred_value_cols[0]
        label_col = actual_value_cols[0]
        merged = predictions.merge(actuals, on=["instrument", "decision_date"], how="inner")
        if merged.empty:
            raise ContractError("no overlapping keys between predictions and actuals")
        results = []
        for metric_def in metric_definitions:
            name = metric_def["name"]
            direction = metric_def["direction"]
            value = MetricReport._compute_single(merged[score_col], merged[label_col], name)
            results.append({
                "name": name,
                "direction": direction,
                "value": value,
                "sample_count": len(merged),
            })

        report = {
            "metric_definitions_used": metric_definitions,
            "results": results,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        return report

    @staticmethod
    def _compute_single(scores: pd.Series, labels: pd.Series, name: str) -> float:
        valid = scores.notna() & labels.notna()
        s = scores[valid]
        y = labels[valid]
        if len(s) < 2:
            raise ContractError("insufficient samples for metric computation")
        if name == "ic":
            return float(s.corr(y, method="pearson"))
        elif name == "rank_ic":
            return float(s.rank().corr(y.rank(), method="pearson"))
        else:
            raise ContractError(f"unsupported metric: {name}")
