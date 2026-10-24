#!/usr/bin/env python3
"""Publish a reviewed cross-sectional preprocessing artifact over a factor window.

`FeaturePreprocessorBuilder` computes the transform and `FeaturePreprocessingStore`
writes it, but nothing joined the two against governed inputs, so a v5 execute had
no way to produce the `feature_preprocessing_binding` it declares. This is that
entry point.

What it deliberately does NOT do: sign the quality review. A publication is only
meaningful if an independent reviewer attested it, so this script reads an
externally produced decision document and refuses to proceed without one. The
repository test key exists for tests; in `UQ_RUNTIME_MODE=production` the bind step
already refuses it (`src/uq/runtime.py:34`).

Ordering is the part that matters. Under `drop_incomplete_rows`, rows whose ordered
features contain a null are removed **before** the transform, so the cross-sectional
statistics come from exactly the rows that will train. This is the same order
Qlib's default learning pipeline uses (`DropnaLabel` then `CSZScoreNorm` in
`learn_processors`). The artifact then pins the *post-drop* frame in
`input_frame_sha256`, which is what the research stage re-checks at execute time.

Label-null filtering does not belong here. Pass the reviewed
`sample_construction` artifact with `--sample-construction-root` and
`--sample-construction-generation`; its selected frame is the transform input.

Usage:
  scripts/publish_feature_preprocessing.py \
      --factor-root data/factors-a500-store \
      --feature-schema-root data/research-root/governed \
      --preprocessing-root data/research-root/governed \
      --factor-set basic --factor-version 2.0.0 \
      --preprocess-name basic-standardized-v1 --semantic-version 1.0.0 \
      --transform standardize_cross_section \
      --drop-incomplete-rows \
      --quality-decisions-json evidence/.../quality-decisions.json \
      --out evidence/.../feature-preprocessing-publication.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.model_layer import ModelContractLoader, model_manifest_identities
from uq.errors import ContractError
from uq.factors.store import FactorStore
from uq.models.feature_preprocessing import FeaturePreprocessorBuilder, complete_rows
from uq.models.features import FeatureSchemaBuilder
from uq.research_chain.owning_contracts import (
    FeaturePreprocessingStore,
    FeatureSchemaStore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _refuse(message: str) -> int:
    print(json.dumps({"status": "configuration_failure", "error": message}, ensure_ascii=False))
    return 2


def _read_partition_set(path: Path) -> list[str]:
    """Factor partition dates from a `factor_partition_set` binding or a bare list."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable factor partition set {path}: {exc}") from exc
    raw = document.get("partitions") if isinstance(document, dict) else document
    if not isinstance(raw, list) or not raw:
        raise SystemExit("factor partition set must be a non-empty list or an object with 'partitions'")
    dates: list[str] = []
    for item in raw:
        value = item.get("partition_date") if isinstance(item, dict) else item
        if not isinstance(value, str):
            raise SystemExit(f"factor partition entry has no date: {item!r}")
        dates.append(value)
    return sorted(set(dates))


def _load_decision(path: Path, family: str) -> dict[str, Any]:
    """Read an externally signed decision for `family` from a decisions document.

    Accepts either the wrapped `{"quality_decisions": {...}}` form the research CLI
    consumes, or a bare decision object.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"unreadable quality decisions {path}: {exc}") from exc
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


def _output_feature_schema(
    frame: pd.DataFrame, *, source_factor_set: str, source_factor_version: str,
    expected_status: str,
) -> dict[str, Any]:
    schema = FeatureSchemaBuilder().build(
        frame, source_factor_set=source_factor_set, source_factor_version=source_factor_version
    )
    for column in schema["columns"]:
        # The builder defaults every column to `identity`; a preprocessed schema has
        # to declare what actually happened to it, or the dataset writer refuses it.
        column["transform_status"] = expected_status
        column["source_factor"] = column["name"]
    schema["generation_id"], schema["manifest_digest_sha256"] = model_manifest_identities(
        schema, schema_name="feature_schema"
    )
    return schema


def _publish_schema(store: FeatureSchemaStore, schema: dict[str, Any]) -> dict[str, Any]:
    """Publish or reuse an immutable feature schema."""
    try:
        return store.publish(schema)
    except ContractError as exc:
        if "already published" not in str(exc):
            raise
        return store.read_schema(schema["generation_id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-root", type=Path, required=True)
    parser.add_argument("--feature-schema-root", type=Path, required=True,
                        help="store root holding feature_schemas/ (usually <data-root>/governed)")
    parser.add_argument("--preprocessing-root", type=Path, required=True,
                        help="store root holding feature_preprocessing/ (usually <data-root>/governed)")
    parser.add_argument("--factor-set", required=True)
    parser.add_argument("--factor-version", required=True)
    parser.add_argument("--preprocess-name", required=True)
    parser.add_argument("--semantic-version", required=True)
    parser.add_argument("--transform", required=True,
                        choices=("standardize_cross_section", "rank_cross_section"))
    parser.add_argument("--min-group-observations", type=int, default=2)
    parser.add_argument(
        "--partition-set",
        type=Path,
        default=None,
        help="factor_partition_set binding JSON (or bare date list) naming the window's partitions",
    )
    parser.add_argument(
        "--partition-date",
        action="append",
        default=None,
        help="factor partition date; repeatable. Alternative to --partition-set.",
    )
    parser.add_argument(
        "--drop-incomplete-rows",
        action="store_true",
        help=(
            "drop rows with a null ordered feature BEFORE the transform. This is what "
            "`missing_policy: drop_incomplete_rows` expects at execute time: the "
            "artifact must attest the post-drop frame, or the stage's fingerprint check fails."
        ),
    )
    parser.add_argument(
        "--sample-construction-root",
        type=Path,
        default=None,
        help=(
            "governed store holding sample_construction/. When provided, the transform "
            "input is exactly that artifact's selected frame; label-null filtering has "
            "already happened there."
        ),
    )
    parser.add_argument(
        "--sample-construction-generation",
        default=None,
        help="generation id of the sample_construction artifact to transform",
    )
    parser.add_argument("--quality-decisions-json", type=Path, required=True,
                        help="externally signed decisions; the script never signs its own review")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if (args.sample_construction_root is None) != (args.sample_construction_generation is None):
        return _refuse(
            "--sample-construction-root and --sample-construction-generation must be provided together"
        )
    if args.sample_construction_root is None:
        if args.partition_set is None and not args.partition_date:
            return _refuse("provide --partition-set or at least one --partition-date")
        if args.partition_set is not None and args.partition_date:
            return _refuse("--partition-set and --partition-date are mutually exclusive")
    elif args.partition_set is not None or args.partition_date:
        return _refuse(
            "sample-construction input and factor partition selection are mutually exclusive"
        )
    dates = (
        _read_partition_set(args.partition_set)
        if args.partition_set is not None
        else sorted(set(args.partition_date))
    ) if args.sample_construction_root is None else []

    registry = FactorRegistry(PROJECT_ROOT)
    store = FactorStore(args.factor_root, registry)

    frames: list[pd.DataFrame] = []
    partition_ids: list[str] = []
    manifests: list[dict[str, Any]] = []
    sample_manifest: dict[str, Any] | None = None
    if args.sample_construction_root is not None:
        from uq.research_chain.owning_contracts import SampleConstructionStore

        sample_manifest, sample_frame = SampleConstructionStore(
            args.sample_construction_root
        ).read_frame(args.sample_construction_generation)
        ordered_features = list(sample_manifest["ordered_features"])
        factor_frame = sample_frame[
            ["instrument", "datetime", *ordered_features]
        ].copy()
        partition_ids = list(sample_manifest["source_factor_generation_ids"])
        for generation_id in partition_ids:
            manifests.append(store.read_manifest(generation_id))
        if sample_manifest["source_factor_set"] != args.factor_set:
            return _refuse(
                "sample construction factor set does not match --factor-set"
            )
        if sample_manifest["source_factor_version"] != args.factor_version:
            return _refuse(
                "sample construction factor version does not match --factor-version"
            )
    for value in dates:
        partition = store.partition_path(
            input_dataset="bars_daily",
            input_schema_version="v2",
            factor_set=args.factor_set,
            factor_version=args.factor_version,
            partition_date=pd.Timestamp(value).date(),
        )
        manifest_path = partition / "manifest.json"
        if not manifest_path.is_file():
            return _refuse(f"no factor partition for {value} under {partition}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _manifest, frame = store.read_partition(manifest["generation_id"])
        frames.append(frame)
        manifests.append(manifest)
        partition_ids.append(manifest["generation_id"])

    if args.sample_construction_root is None:
        factor_frame = pd.concat(frames, ignore_index=True).sort_values(
            ["instrument", "datetime"], kind="mergesort"
        ).reset_index(drop=True)
    ordered_features = (
        list(sample_manifest["ordered_features"])
        if sample_manifest is not None
        else [c for c in factor_frame.columns if c not in ("instrument", "datetime")]
    )
    if not ordered_features:
        return _refuse("factor frame has no feature columns")

    dropped: dict[str, Any] = {}
    if sample_manifest is not None and args.drop_incomplete_rows:
        return _refuse(
            "--drop-incomplete-rows is invalid with a sample-construction input; "
            "row selection already happened there"
        )
    if sample_manifest is not None and factor_frame[ordered_features].isna().any().any():
        return _refuse(
            "sample construction contains null features; the selection policy should "
            "have removed them"
        )
    if args.drop_incomplete_rows:
        incomplete = factor_frame[ordered_features].isna().any(axis=1)
        if incomplete.any():
            kept = complete_rows(factor_frame, ordered_features)
            dropped = {
                "rows": int(len(factor_frame) - len(kept)),
                "instruments_fully_dropped": int(
                    len(set(factor_frame["instrument"]) - set(kept["instrument"]))
                ),
                "per_feature": {
                    name: int(count)
                    for name, count in factor_frame[ordered_features].isna().sum().items()
                    if count
                },
            }
            factor_frame = kept
    elif factor_frame[ordered_features].isna().any().any():
        # Without the flag the transform preserves nulls, which the dataset stage
        # cannot consume under fail_closed. Refuse rather than publish an artifact
        # whose only consumer will reject it.
        counts = {
            name: int(count)
            for name, count in factor_frame[ordered_features].isna().sum().items()
            if count
        }
        return _refuse(
            "factor frame contains null features; pass --drop-incomplete-rows or fix the input: "
            f"{counts}"
        )

    builder = FeaturePreprocessorBuilder(
        preprocess_name=args.preprocess_name,
        semantic_version=args.semantic_version,
        transform=args.transform,
        min_group_observations=args.min_group_observations,
    )
    transformed = builder.transform(factor_frame, ordered_features)

    source_factor_set = manifests[0]["factor_set"]
    source_factor_version = manifests[0]["factor_version"]
    input_schema = _publish_schema(
        FeatureSchemaStore(args.feature_schema_root),
        FeatureSchemaBuilder().build(
            factor_frame, source_factor_set=source_factor_set,
            source_factor_version=source_factor_version,
        ),
    )
    output_schema = _publish_schema(
        FeatureSchemaStore(args.feature_schema_root),
        _output_feature_schema(
            transformed, source_factor_set=source_factor_set,
            source_factor_version=source_factor_version,
            expected_status=(
                "standardized" if args.transform == "standardize_cross_section" else "ranked"
            ),
        ),
    )

    try:
        manifest = builder.build(
            factor_frame, transformed,
            input_factor_set=source_factor_set,
            input_factor_version=source_factor_version,
            input_factor_generation_ids=partition_ids,
            ordered_features=ordered_features,
            input_feature_schema=input_schema,
            output_feature_schema=output_schema,
        )
    except ContractError as exc:
        return _refuse(f"preprocessing build refused: {exc}")

    try:
        decision = _load_decision(args.quality_decisions_json, "feature_preprocessing")
    except ContractError as exc:
        return _refuse(str(exc))
    try:
        published = FeaturePreprocessingStore(args.preprocessing_root).publish(
            manifest, quality_decision=decision
        )
    except ContractError as exc:
        if "already published" in str(exc):
            published = FeaturePreprocessingStore(args.preprocessing_root).read_manifest(
                manifest["generation_id"]
            )
        else:
            return _refuse(f"preprocessing publication refused: {exc}")

    report = {
        "record_version": 1,
        "kind": "feature_preprocessing_publication",
        "window": {
            "partitions": dates,
            "sessions": len(dates) if dates else None,
            "sample_construction": (
                None if sample_manifest is None
                else {
                    "window_start": sample_manifest["window_start"],
                    "window_end": sample_manifest["window_end"],
                }
            ),
        },
        "factor": {"set": source_factor_set, "version": source_factor_version,
                   "generation_ids": partition_ids},
        "transform": args.transform,
        "preprocess_name": args.preprocess_name,
        "semantic_version": args.semantic_version,
        "ordered_features": ordered_features,
        "rows": int(len(factor_frame)),
        "instruments": int(factor_frame["instrument"].nunique()),
        "sample_construction_generation_id": (
            None if sample_manifest is None else sample_manifest["generation_id"]
        ),
        "dropped_incomplete_rows": dropped,
        "drop_incomplete_rows_enabled": bool(args.drop_incomplete_rows),
        "input_frame_sha256": manifest["input_frame_sha256"],
        "output_frame_sha256": manifest["output_frame_sha256"],
        "generation_id": published["generation_id"],
        "manifest_digest_sha256": published["manifest_digest_sha256"],
        "binding": {
            "family": "feature_preprocessing",
            "generation_id": published["generation_id"],
            "manifest_digest_sha256": published["manifest_digest_sha256"],
        },
        "review": {
            "source": str(args.quality_decisions_json),
            "status": decision.get("status"),
            "signed_by_script": False,
            "note": (
                "the decision is read from an external document; this entry point never "
                "signs a review of its own output"
            ),
        },
        "stores": {
            "factor_root": str(args.factor_root),
            "feature_schema_root": str(args.feature_schema_root),
            "preprocessing_root": str(args.preprocessing_root),
            "tracked": False,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "generation_id": published["generation_id"],
        "rows": len(factor_frame),
        "instruments": int(factor_frame["instrument"].nunique()),
        "dropped": dropped.get("rows", 0),
        "out": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
