"""Portfolio layer: convert prediction scores into governed target weights."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import pyarrow as arrow
import pyarrow.parquet as parquet

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.gate_contracts import validate_contract
from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    model_manifest_identities,
    resolve_subject_quality_decision,
    reviewed_allowed_statuses,
    sha256_json,
    validate_quality_decision_owning_report,
)
from ..errors import ContractError

_WEIGHT_TOLERANCE = 1e-8


def _selected_industries(
    selected: list[str], industry_by_instrument: Mapping[str, str] | None
) -> dict[str, str]:
    if industry_by_instrument is None:
        raise ContractError("industry-aware portfolio requires point-in-time industry facts")
    labels: dict[str, str] = {}
    for instrument in selected:
        value = industry_by_instrument.get(instrument)
        if not isinstance(value, str) or not value.strip():
            raise ContractError(
                f"industry facts are missing for selected instrument {instrument}"
            )
        labels[instrument] = value.strip()
    return labels


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
        initial_state: bool = False,
        quality_decision: dict[str, Any] | None = None,
        risk_return_frame: pd.DataFrame | None = None,
        risk_return_calendar_sessions: list[str] | None = None,
        risk_return_manifest: dict[str, Any] | None = None,
        industry_by_instrument: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], pd.DataFrame]:
        """Compute target weights for one rebalance date.

        Args:
            definition: validated portfolio_definition.v1, v2, or v3 manifest.
            prediction_generation_id: upstream prediction generation.
            decision_date: ISO date string.
            scores: instrument-indexed score series (already filtered to eligible).
            universe_instruments: allowed instruments from the universe snapshot.
            previous_target_weights: prior date's {instrument: weight} mapping.
                Required whenever ``max_turnover`` is set, unless this partition is the
                run's initialization.
            initial_state: states that there is no earlier governed target state, so
                the turnover cap has nothing to measure against and is not applied.
                Only the first partition of a run qualifies; the published manifest
                records ``previous_target_weights_generation_id: null``, which is what
                makes the exemption auditable instead of silent.
            quality_decision: externally reviewed quality decision document.
            risk_return_frame / risk_return_calendar_sessions: governed daily returns
                and their bound trading calendar, required by risk parity v3.
            risk_return_manifest: verified manifest read from the owning risk-return
                store, required to match the portfolio definition's binding.
            industry_by_instrument: point-in-time industry labels read from the
                reviewed execution-facts manifest, required for industry-neutral
                weighting or an industry cap below 100%.

        Returns:
            (manifest, frame) where frame has columns [instrument, weight].
        """
        contract_version = definition.get("contract_version", 1)
        weight_scheme = definition.get("weight_scheme")
        supported_schemes = (
            {"top_n_equal_weight"}
            if contract_version == 1
            else {"top_n_equal_weight", "score_weighted"}
            if contract_version == 2
            else {"top_n_equal_weight", "score_weighted", "risk_parity", "industry_neutral"}
            if contract_version == 3
            else set()
        )
        if weight_scheme not in supported_schemes:
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

        selected_rows = ranked.head(n)
        if weight_scheme == "top_n_equal_weight":
            # Equal weight with single-position cap.
            raw_weight = 1.0 / n
            capped_weight = min(raw_weight, max_single)
            weights_map = {inst: capped_weight for inst in selected}
            total_stock = capped_weight * n
        elif weight_scheme == "score_weighted":
            selected_scores = selected_rows["score"].to_numpy(dtype=float)
            if not np.isfinite(selected_scores).all():
                raise ContractError("score_weighted requires finite scores for selected instruments")
            if "temperature" not in scheme_params:
                raise ContractError("score_weighted requires a temperature parameter")
            temperature = float(scheme_params["temperature"])
            if not np.isfinite(temperature) or temperature <= 0:
                raise ContractError("score_weighted temperature must be finite and positive")
            logits = (selected_scores - selected_scores.max()) / temperature
            raw = np.exp(np.maximum(logits, -700.0))
            total = float(raw.sum())
            if not np.isfinite(total) or total <= 0:
                raise ContractError("score_weighted could not normalize selected scores")
            normalized = raw / total
            weights_map = {
                inst: min(float(weight), max_single)
                for inst, weight in zip(selected, normalized, strict=True)
            }
            total_stock = sum(weights_map.values())
        elif weight_scheme == "risk_parity":
            risk_binding = definition.get("risk_model_binding")
            selected_scores = selected_rows["score"].to_numpy(dtype=float)
            if not np.isfinite(selected_scores).all():
                raise ContractError("risk_parity requires finite selected scores")
            if (not isinstance(risk_return_frame, pd.DataFrame)
                    or set(risk_return_frame.columns) != {"date", "instrument", "return"}
                    or risk_return_frame.duplicated(["date", "instrument"]).any()
                    or risk_return_calendar_sessions is None
                    or risk_return_manifest is None):
                raise ContractError("risk_parity requires its governed daily returns and calendar")
            validate_contract("portfolio_risk_returns.v1.json", risk_return_manifest)
            risk_payload = {
                key: value for key, value in risk_return_manifest.items()
                if key not in {"generation_id", "manifest_digest_sha256"}
            }
            if (sha256_json(risk_payload) != risk_return_manifest["generation_id"]
                    or sha256_json({**risk_payload, "generation_id": risk_return_manifest["generation_id"]})
                    != risk_return_manifest["manifest_digest_sha256"]):
                raise ContractError("risk_parity return manifest identity is invalid")
            if (not isinstance(risk_binding, dict)
                    or risk_binding.get("family") != "portfolio_risk_returns_v1"
                    or risk_binding.get("generation_id") != risk_return_manifest.get("generation_id")
                    or risk_binding.get("manifest_digest_sha256")
                    != risk_return_manifest.get("manifest_digest_sha256")):
                raise ContractError("risk_parity return manifest does not match its reviewed binding")
            if (risk_return_manifest.get("calendar_sessions") != risk_return_calendar_sessions
                    or len(risk_return_frame) != risk_return_manifest.get("row_count")
                    or risk_return_manifest.get("observation_visibility")
                    != "available_after_session_close"
                    or risk_return_manifest.get("strict_prior_session_policy") is not True):
                raise ContractError("risk_parity return manifest has an invalid calendar or visibility policy")
            from .risk_model import (
                covariance_for_decision,
                daily_returns_fingerprint,
                risk_parity_weights,
            )

            if daily_returns_fingerprint(risk_return_frame) != risk_return_manifest.get(
                "logical_checksum_sha256"
            ):
                raise ContractError("risk_parity return data does not match its manifest")

            lookback_sessions = scheme_params.get("lookback_sessions")
            covariance = covariance_for_decision(
                risk_return_frame,
                calendar_sessions=risk_return_calendar_sessions,
                decision_date=decision_date,
                instruments=selected,
                lookback_sessions=lookback_sessions,
            )
            raw_weights = risk_parity_weights(covariance)
            weights_map = {
                instrument: min(float(weight), max_single)
                for instrument, weight in zip(selected, raw_weights, strict=True)
            }
            total_stock = sum(weights_map.values())
        else:
            selected_scores = selected_rows["score"].to_numpy(dtype=float)
            if not np.isfinite(selected_scores).all():
                raise ContractError("industry_neutral requires finite selected scores")
            labels = _selected_industries(selected, industry_by_instrument)
            groups: dict[str, list[str]] = {}
            for instrument in selected:
                groups.setdefault(labels[instrument], []).append(instrument)
            industry_budget = 1.0 / len(groups)
            weights_map = {
                instrument: min(industry_budget / len(members), max_single)
                for members in groups.values()
                for instrument in members
            }
            total_stock = sum(weights_map.values())

        # Cash reserve: scale down if needed
        max_allowed_total = 1.0 - cash_reserve + _WEIGHT_TOLERANCE
        if total_stock > max_allowed_total:
            scale_factor = max_allowed_total / total_stock
            weights_map = {
                instrument: weight * scale_factor
                for instrument, weight in weights_map.items()
            }
            total_stock = sum(weights_map.values())

        # Industry cap requires a governed manifest runtime; private field bypass is forbidden.
        industry_cap_applied = max_industry is not None and max_industry < 1.0
        if industry_cap_applied:
            industry_binding = definition.get("industry_source_binding")
            if contract_version < 3:
                if (not industry_binding
                        or industry_binding.get("source_type") != "governed_industry_manifest"):
                    raise ContractError(
                        "max_industry_weight requires a governed_industry_manifest binding"
                    )
                raise ContractError(
                    "industry cap requires a governed industry membership runtime, which is not enabled"
                )
            if (not industry_binding
                    or industry_binding.get("source_type") != "governed_execution_facts"
                    or industry_by_instrument is None):
                raise ContractError(
                    "max_industry_weight requires bound point-in-time industry facts"
                )
            labels = _selected_industries(selected, industry_by_instrument)
            industry_weights: dict[str, float] = {}
            for instrument, industry in labels.items():
                industry_weights[industry] = industry_weights.get(industry, 0.0) + weights_map.get(instrument, 0.0)
            for industry, exposure in industry_weights.items():
                if exposure > max_industry + _WEIGHT_TOLERANCE:
                    scale_factor = max_industry / exposure
                    for instrument in selected:
                        if labels[instrument] == industry:
                            weights_map[instrument] *= scale_factor
            total_stock = sum(weights_map.values())

        # Turnover cap.
        #
        # The cap is defined against the previous *target* weights, so it needs a
        # stated answer for "what came before".  Silently skipping it was the real
        # defect: the first rebalance of any run had no previous frame, so every
        # ``max_turnover > 0`` definition died on day one, while ``max_turnover == 0``
        # slipped past both guards and built a full position that a 0.0001 cap would
        # have rejected (external review B10a/B10b).
        if max_turnover is not None:
            if previous_target_weights is None:
                if not initial_state:
                    raise ContractError(
                        "previous_target_weights is required when max_turnover is set; "
                        "pass initial_state=True only for the first partition of a run"
                    )
            else:
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

        # Turnover blending toward prior holdings can reintroduce an industry
        # exposure that the current target just scaled down. Never publish that
        # conflict as a satisfied sector cap.
        if industry_cap_applied and weights_map:
            final_labels = _selected_industries(list(weights_map), industry_by_instrument)
            final_industry_weights: dict[str, float] = {}
            for instrument, industry in final_labels.items():
                final_industry_weights[industry] = (
                    final_industry_weights.get(industry, 0.0) + weights_map[instrument]
                )
            if any(
                exposure > max_industry + _WEIGHT_TOLERANCE
                for exposure in final_industry_weights.values()
            ):
                raise ContractError(
                    "turnover constraint conflicts with the maximum industry weight"
                )

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
            # cr.md U15: accept the raw signed decision shape as well — bind it
            # to this definition on the spot (envelopes keep the legacy path).
            bound_report, _ = bind_reviewed_quality_decision(
                dict(quality_decision),
                binding_type="portfolio_definition_v1",
                subject_generation_id=definition["generation_id"],
                subject_content_sha256=definition["manifest_digest_sha256"],
            )
            return definition, bound_report
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
        quality_decision = resolve_subject_quality_decision(
            quality_decision, subject_generation_id=provisional_gen, subject_content_sha256=checksum
        )
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
        if report.get("status") not in reviewed_allowed_statuses("target_weights_v1"):
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
