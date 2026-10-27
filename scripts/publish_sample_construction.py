#!/usr/bin/env python3
"""Publish the reviewed training-sample selection for a research window.

This is the production entry point for `sample_construction.v1`. It reads the
same governed inputs the dataset stage consumes (factor partitions, label set,
universe and index-membership set), applies the reviewed row policy, and writes
one immutable frame containing keys, ordered features and label.

Feature preprocessing is intentionally not involved: this artifact answers
"which rows are trainable?", while preprocessing answers "what stateless
cross-sectional transform is applied to those rows?".

Usage:
  scripts/publish_sample_construction.py \
      --factor-root data/bars-csiall-store \
      --label-root data/bars-csiall-store \
      --universe-root data/bars-csiall-store \
      --sample-construction-root data/bars-csiall-store/governed \
      --membership-root data/csi-all-store \
      --market-dataset-root data/market-datasets-0930 \
      --factor-partition-set evidence/.../factor-partition-set.json \
      --universe-id csi-all \
      --label-generation <generation> \
      --universe-generation <generation> \
      --missing-policy drop_unlabeled_rows \
      --sample-name full-a-2025-09 \
      --semantic-version 1.0.0 \
      --quality-decisions-json evidence/.../decisions.json \
      --out evidence/.../sample-construction.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from uq.contracts.artifacts import UniverseSnapshotStore
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.index_membership import IndexMembershipStore
from uq.errors import ContractError
from uq.execution.market_dataset_publisher import MarketDatasetPublisher
from uq.execution.market_source import PublishedPaperMarketData
from uq.factors.store import FactorStore
from uq.models.sample_construction import SampleConstructionBuilder, select_training_sample
from uq.research_chain.membership_input import filtered_factor_frame, verify_request_input_sets
from uq.research_chain.owning_contracts import LabelStore, SampleConstructionStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _refuse(message: str) -> int:
    print(json.dumps({"status": "configuration_failure", "error": message}, ensure_ascii=False))
    return 2


def _load_decision(path: Path, family: str, subject_generation_id: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"unreadable quality decisions {path}: {exc}") from exc
    if isinstance(document, dict) and "provider_command" in document:
        command = document["provider_command"]
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ContractError("provider_command must be a non-empty list of strings")
        if document.get("provider_id") != "external-model-quality-reviewer-v1":
            raise ContractError("unsupported decision provider identity")
        from uq.research_chain.cli import _CommandDecisionProvider, _decision_provider_for_family

        provider = _CommandDecisionProvider(
            command=command,
            provider_config_ref=document.get(
                "provider_config_ref", "sample-construction-publisher"
            ),
        )
        return dict(
            _decision_provider_for_family(provider, family)(
                subject_generation_id, subject_generation_id
            )
        )
    if isinstance(document, dict) and "quality_decisions" in document:
        decisions = document["quality_decisions"]
        if not isinstance(decisions, dict):
            raise ContractError("quality_decisions must be an object")
        decision = decisions.get(family)
        if decision is None:
            raise ContractError(f"quality decisions document has no entry for {family}")
        return decision
    if isinstance(document, dict) and document.get("binding_type"):
        return document
    raise ContractError(
        f"quality decisions {path} is neither a wrapped document nor a decision object"
    )


def _partition_set(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"unreadable factor partition set {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("partitions"), list):
        raise ContractError("factor partition set must be an object with partitions")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-root", type=Path, required=True)
    parser.add_argument("--label-root", type=Path, required=True)
    parser.add_argument("--universe-root", type=Path, required=True)
    parser.add_argument(
        "--sample-construction-root",
        type=Path,
        required=True,
        help=(
            "governed store that holds sample_construction/. This is NOT the factor "
            "root: the research chain builds `SampleConstructionStore` under "
            "`<data-root>/governed`, so publishing beside the factor partitions would "
            "write an artifact the v6 dataset stage can never resolve."
        ),
    )
    parser.add_argument("--membership-root", type=Path, required=True)
    parser.add_argument("--market-dataset-root", type=Path, required=True)
    parser.add_argument("--factor-partition-set", type=Path, required=True)
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--label-generation", required=True)
    parser.add_argument("--universe-generation", required=True)
    parser.add_argument(
        "--missing-policy",
        required=True,
        choices=("fail_closed", "drop_incomplete_rows", "drop_unlabeled_rows"),
    )
    parser.add_argument("--sample-name", required=True)
    parser.add_argument("--semantic-version", required=True)
    parser.add_argument("--quality-decisions-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    partition_set = _partition_set(args.factor_partition_set)
    window_start = date.fromisoformat(partition_set["window_start"])
    window_end = date.fromisoformat(partition_set["window_end"])
    membership_store = IndexMembershipStore(args.membership_root)
    market_publisher = MarketDatasetPublisher(args.market_dataset_root)
    trading_days = PublishedPaperMarketData(
        market_publisher, window_start=window_start, window_end=window_end
    ).sessions()
    factor_store = FactorStore(args.factor_root, FactorRegistry(PROJECT_ROOT))
    request = {
        "window_start_date": window_start.isoformat(),
        "window_end_date": window_end.isoformat(),
        "factor_partition_set_binding": {
            key: partition_set[key]
            for key in (
                "binding_type",
                "input_dataset",
                "input_schema_version",
                "factor_set",
                "factor_version",
                "window_start",
                "window_end",
                "set_digest_sha256",
            )
        },
        "index_membership_binding": {
            "binding_type": "index_membership_set",
            "universe_id": args.universe_id,
            "index_code": "",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "set_digest_sha256": "",
        },
    }
    # The membership set digest is recomputed from the store, so a caller does
    # not need a second hand-maintained digest. Derive it from the same helper
    # the resolver uses and bind the result into the sample manifest.
    from uq.contracts.index_membership import build_membership_set

    membership_set = build_membership_set(
        membership_store,
        universe_id=args.universe_id,
        window_start=window_start,
        window_end=window_end,
        trading_days=trading_days,
    )
    request["index_membership_binding"]["set_digest_sha256"] = membership_set[
        "set_digest_sha256"
    ]
    request["index_membership_binding"]["index_code"] = membership_set["index_code"]
    verified_sets = verify_request_input_sets(
        request,
        membership_store=membership_store,
        factor_store=factor_store,
        trading_days=trading_days,
    )
    factor_frame = filtered_factor_frame(
        request,
        membership_store=membership_store,
        factor_store=factor_store,
        trading_days=trading_days,
        verified=verified_sets,
    )
    ordered_features = [
        column for column in factor_frame.columns
        if column not in {"instrument", "datetime"}
    ]
    if not ordered_features:
        return _refuse("filtered factor frame has no feature columns")

    label_manifest, label_frame = LabelStore(args.label_root).read_frame(
        args.label_generation
    )
    universe_manifest = UniverseSnapshotStore(args.universe_root).read_manifest(
        args.universe_generation
    )
    if universe_manifest.get("universe_id") != args.universe_id:
        return _refuse("universe snapshot universe_id does not match --universe-id")

    sample_frame, dropped_rows, dropped_labels = select_training_sample(
        factor_frame,
        label_frame,
        ordered_features=ordered_features,
        missing_policy=args.missing_policy,
    )
    builder = SampleConstructionBuilder(
        sample_name=args.sample_name,
        semantic_version=args.semantic_version,
    )
    manifest = builder.build(
        sample_frame,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        source_factor_set=partition_set["factor_set"],
        source_factor_version=partition_set["factor_version"],
        source_factor_generation_ids=[
            entry["generation_id"] for entry in partition_set["partitions"]
        ],
        source_factor_partition_set_digest_sha256=partition_set["set_digest_sha256"],
        source_label_set_name=label_manifest["name"],
        source_label_generation_id=label_manifest["generation_id"],
        source_label_manifest_digest_sha256=label_manifest["manifest_digest_sha256"],
        source_label_data_checksum_sha256=label_manifest["data_checksum_sha256"],
        source_universe_snapshot_generation_id=universe_manifest["generation_id"],
        source_index_membership_set_digest_sha256=membership_set["set_digest_sha256"],
        ordered_features=ordered_features,
        missing_policy=args.missing_policy,
        rows_before_selection=len(factor_frame),
        dropped_rows=dropped_rows,
        dropped_labels=dropped_labels,
    )
    try:
        decision = _load_decision(
            args.quality_decisions_json,
            "sample_construction",
            manifest["generation_id"],
        )
    except ContractError as exc:
        return _refuse(str(exc))
    try:
        published = SampleConstructionStore(args.sample_construction_root).publish(
            manifest, sample_frame, quality_decision=decision
        )
    except ContractError as exc:
        if "already published" in str(exc):
            published = SampleConstructionStore(args.sample_construction_root).read_manifest(
                manifest["generation_id"]
            )
        else:
            return _refuse(f"sample construction publication refused: {exc}")

    report = {
        "record_version": 1,
        "kind": "sample_construction_publication",
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "sample_name": args.sample_name,
        "semantic_version": args.semantic_version,
        "missing_policy": args.missing_policy,
        "rows_before_selection": len(factor_frame),
        "rows_after_selection": len(sample_frame),
        "instruments": int(sample_frame["instrument"].nunique()),
        "dropped_rows": dropped_rows,
        "dropped_labels": dropped_labels,
        "generation_id": published["generation_id"],
        "manifest_digest_sha256": published["manifest_digest_sha256"],
        "binding": {
            "family": "sample_construction",
            "generation_id": published["generation_id"],
            "manifest_digest_sha256": published["manifest_digest_sha256"],
        },
        "review": {
            "source": str(args.quality_decisions_json),
            "status": decision.get("status"),
            "signed_by_script": False,
        },
        "stores": {
            "factor_root": str(args.factor_root),
            "label_root": str(args.label_root),
            "universe_root": str(args.universe_root),
            "sample_construction_root": str(args.sample_construction_root),
            "membership_root": str(args.membership_root),
            "tracked": False,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "generation_id": published["generation_id"],
        "rows": len(sample_frame),
        "instruments": int(sample_frame["instrument"].nunique()),
        "out": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
