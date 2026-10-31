"""Reviewed v3 cost binding for rough factor turnover cost diagnostics."""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    model_manifest_identities,
)
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..research_chain.owning_contracts import BacktestConfigStore


def reviewed_v3_turnover_cost_proxy(
    root: Path | str,
    generation: str | None,
    *,
    start: str,
    end: str,
) -> tuple[dict | None, float | None]:
    """Return a signed v3 config binding and bounded one-way turnover bps proxy."""
    root = Path(root)
    if generation is None:
        return None, None
    config = BacktestConfigStore(root).read_manifest(generation)
    if config.get("contract_version") != 3:
        raise ContractError("factor turnover cost proxy requires a reviewed backtest config v3")
    ModelContractLoader.validate("backtest_config_v3", config)
    expected_generation, expected_digest = model_manifest_identities(
        config, schema_name="backtest_config_v3"
    )
    if (expected_generation != config["generation_id"]
            or expected_digest != config["manifest_digest_sha256"]):
        raise ContractError("factor screen backtest config identity mismatch")
    if config["start_date"] > start or config["end_date"] < end:
        raise ContractError("factor screen cost config must cover the full diagnostic window")
    config_path = root / "backtest_configs" / f"generation={generation}" / "manifest.json"
    reject_symlinks_within(root, config_path, label="factor screen cost config")
    review_path = root / "external_quality_reviews" / f"{config['quality_report_checksum_sha256']}.json"
    reject_symlinks_within(root, review_path, label="factor screen cost config review")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("factor screen reviewed cost config receipt is unavailable or malformed") from exc
    decision = {key: value for key, value in review.items()
                if key not in {"bound_generation_id", "report_checksum_sha256"}}
    decision["subject_generation_id"] = review.get("bound_generation_id")
    provisional, _ = model_manifest_identities(
        {**config, "quality_report_checksum_sha256": "0" * 64},
        schema_name="backtest_config_v3",
    )
    verified_review, verified_checksum = bind_reviewed_quality_decision(
        decision,
        binding_type="backtest_config_v1",
        subject_generation_id=provisional,
        subject_content_sha256=provisional,
    )
    if (verified_checksum != config["quality_report_checksum_sha256"]
            or verified_review != review):
        raise ContractError("factor screen cost config review binding mismatch")

    costs = config["cost_model"]
    execution = config["execution_model"]
    if execution["fill_rate_multiplier"] == 0:
        raise ContractError("factor turnover cost proxy is undefined when the configured fill rate is zero")
    impact = costs["impact_model"]
    participation_cap = execution["volume_participation_cap"] * execution["fill_rate_multiplier"]
    participation_measure = (
        math.sqrt(participation_cap) if impact["type"] == "square_root" else participation_cap
    )
    impact_at_cap_bps = min(
        impact["maximum_bps"], impact["coefficient_bps"] * participation_measure
    )
    proxy_bps = (
        2 * costs["commission_bps"]
        + costs["stamp_duty_bps"]
        + 2 * costs["slippage_bps"]
        + 2 * impact_at_cap_bps
    )
    if not math.isfinite(proxy_bps) or proxy_bps < 0:
        raise ContractError("factor screen cost proxy is non-finite or negative")
    binding = {
        "generation_id": config["generation_id"],
        "manifest_digest_sha256": config["manifest_digest_sha256"],
        "start_date": config["start_date"],
        "end_date": config["end_date"],
        "commission_bps": costs["commission_bps"],
        "stamp_duty_bps": costs["stamp_duty_bps"],
        "slippage_bps": costs["slippage_bps"],
        "minimum_commission_cny": costs["minimum_commission_cny"],
        "impact_type": impact["type"],
        "impact_coefficient_bps": impact["coefficient_bps"],
        "impact_maximum_bps": impact["maximum_bps"],
        "impact_at_participation_cap_bps": impact_at_cap_bps,
        "volume_participation_cap": execution["volume_participation_cap"],
        "fill_rate_multiplier": execution["fill_rate_multiplier"],
    }
    return binding, proxy_bps
