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
        serializer_version: str = "json-numpy-v1",
    ) -> dict[str, Any]:
        if algorithm not in {"regularized_linear", "qlib_linear"}:
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
            "run_id": "00000000-0000-4000-8000-000000000000",
            "created_at": "1970-01-01T00:00:00+00:00",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id, manifest_digest = model_manifest_identities(
            manifest,
            schema_name="model_definition",
            exclude_fields={"quality_report_checksum_sha256", "model_run_content_generation_id"},
        )
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("model_definition", manifest)
        return manifest


class MetricReport:
    """Compute and serialize metric values from predictions vs actuals.

    The IC family is computed **cross-sectionally by default**: each decision date
    is one cross-section, the per-date correlation is taken, and the metric is the
    unweighted mean over the dates that carry a usable cross-section.  This removes
    the day-over-day common move (market beta) that a single pooled correlation
    would fold into the score, so the value measures cross-sectional selection
    skill rather than selection plus timing.

    A pooled correlation over the whole frame remains available as an explicit
    opt-in by naming the metric ``ic_pooled`` / ``rank_ic_pooled``.  ``ic`` and
    ``rank_ic`` are the cross-sectional variants.  The metric ``name`` is the
    contract surface (the schema leaves it a free string), so the basis is always
    visible in the report's ``basis`` field and never inferred silently.
    """

    # name -> (basis, correlation_kind)
    _METRIC_BASIS = {
        "ic": ("cross_sectional", "pearson"),
        "rank_ic": ("cross_sectional", "rank"),
        "ic_pooled": ("pooled", "pearson"),
        "rank_ic_pooled": ("pooled", "rank"),
    }

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
            basis, correlation_kind = MetricReport._resolve_metric(name)
            value, cross_section_count = MetricReport._compute(
                merged, score_col=score_col, label_col=label_col,
                basis=basis, correlation_kind=correlation_kind,
            )
            results.append({
                "name": name,
                "direction": direction,
                "basis": basis,
                "value": value,
                "sample_count": len(merged),
                "cross_section_count": cross_section_count,
            })

        report = {
            "metric_definitions_used": metric_definitions,
            "results": results,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        return report

    @staticmethod
    def _resolve_metric(name: str) -> tuple[str, str]:
        try:
            return MetricReport._METRIC_BASIS[name]
        except KeyError as exc:
            raise ContractError(f"unsupported metric: {name}") from exc

    @staticmethod
    def _correlate(scores: pd.Series, labels: pd.Series, correlation_kind: str) -> float:
        if correlation_kind == "rank":
            scores = scores.rank()
            labels = labels.rank()
        return float(scores.corr(labels, method="pearson"))

    @staticmethod
    def _compute(
        merged: pd.DataFrame,
        *,
        score_col: str,
        label_col: str,
        basis: str,
        correlation_kind: str,
    ) -> tuple[float, int]:
        if basis == "pooled":
            valid = merged[score_col].notna() & merged[label_col].notna()
            scores = merged.loc[valid, score_col]
            labels = merged.loc[valid, label_col]
            if len(scores) < 2:
                raise ContractError("insufficient samples for metric computation")
            return MetricReport._correlate(scores, labels, correlation_kind), 0
        if basis != "cross_sectional":
            raise ContractError(f"unsupported metric basis: {basis}")

        per_date: list[float] = []
        for _, cross_section in merged.groupby("decision_date", sort=True):
            valid = cross_section[score_col].notna() & cross_section[label_col].notna()
            scores = cross_section.loc[valid, score_col]
            labels = cross_section.loc[valid, label_col]
            if len(scores) < 2:
                continue
            # A constant cross-section has an undefined correlation; skip it up
            # front (and avoid the numpy divide-by-zero warning) rather than let
            # it poison the mean with NaN.
            if scores.nunique() < 2 or labels.nunique() < 2:
                continue
            per_date.append(MetricReport._correlate(scores, labels, correlation_kind))
        if not per_date:
            raise ContractError("no usable cross-section for metric computation")
        return float(sum(per_date) / len(per_date)), len(per_date)
