from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import pandas as pd
import pytest

from uq.contracts.model_layer import (
    ModelContractLoader,
    create_reviewed_quality_decision,
    model_manifest_identities,
    sha256_json,
)
from uq.errors import ContractError
from uq.models.dataset import DatasetBuilder
from uq.models.dataset_writer import DatasetWriter
from uq.models.feature_preprocessing import FeaturePreprocessorBuilder
from uq.models.features import FeatureSchemaBuilder
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.factors.raw_price import logical_fingerprint as frame_logical_fingerprint

CHECKS = {'model_dataset_v1': [{'name': 'row_count', 'threshold': 0, 'observed': 16, 'level': 'error', 'result': 'passed'}, {'name': 'schema_reconciliation', 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}, {'name': 'readback_reconciliation', 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}, {'name': 'null_rate', 'threshold': 0.1, 'observed': 0.0, 'level': 'error', 'result': 'passed'}], 'feature_preprocessing_v1': [{'name': 'key_reconciliation', 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}, {'name': 'row_count_reconciliation', 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}, {'name': 'output_readback', 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}, {'name': 'null_rate', 'threshold': 0.1, 'observed': 0.0, 'level': 'error', 'result': 'passed'}]}


def _frame() -> pd.DataFrame:
    rows = []
    for day, values in [
        (datetime(2026, 1, 5), [1.0, 2.0, 3.0, 4.0]),
        (datetime(2026, 1, 6), [2.0, 4.0, 6.0, 8.0]),
        (datetime(2026, 1, 7), [4.0, 1.0, 3.0, 2.0]),
        (datetime(2026, 1, 8), [5.0, 6.0, 7.0, 8.0]),
    ]:
        for index, value in enumerate(values):
            rows.append({
                "instrument": f"60000{index}.XSHG",
                "datetime": pd.Timestamp(day),
                "volume_ratio_20d": value,
            })
    return pd.DataFrame(rows)


def _report() -> dict:
    return create_reviewed_quality_decision(
        binding_type="feature_preprocessing_v1",
        policy="cross_sectional_stateless_v1",
        status="passed",
        checks=CHECKS["feature_preprocessing_v1"],
        errors=[],
        warnings=[],
        producer_code_fingerprint="0" * 64,
    )


def _dataset_report() -> dict:
    return create_reviewed_quality_decision(
        binding_type="model_dataset_v1",
        policy="reject_all",
        status="passed",
        checks=CHECKS["model_dataset_v1"],
        errors=[],
        warnings=[],
        producer_code_fingerprint="0" * 64,
    )


def _input_schema(frame: pd.DataFrame) -> dict:
    schema = FeatureSchemaBuilder().build(
        frame, source_factor_set="basic", source_factor_version="1.0.0"
    )
    ModelContractLoader.validate("feature_schema", schema)
    return schema


def _standardized(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    grouped = output.groupby("datetime", sort=False)["volume_ratio_20d"]
    output["volume_ratio_20d"] = grouped.transform(lambda values: (values - values.mean()) / values.std(ddof=0))
    return output.astype({"volume_ratio_20d": "float64"})


def _output_schema(output_frame: pd.DataFrame) -> dict:
    schema = FeatureSchemaBuilder().build(
        output_frame, source_factor_set="basic", source_factor_version="1.0.0"
    )
    for column in schema["columns"]:
        column["transform_status"] = "standardized"
        column["source_factor"] = "volume_ratio_20d"
    generation_id, digest = model_manifest_identities(schema, schema_name="feature_schema")
    schema["generation_id"] = generation_id
    schema["manifest_digest_sha256"] = digest
    ModelContractLoader.validate("feature_schema", schema)
    return schema


def _dataset_manifest(
    output_frame: pd.DataFrame,
    preprocessing: dict | None = None,
    input_feature_schema: dict | None = None,
) -> dict:
    digest = "0" * 64
    return DatasetBuilder(dataset_name="preprocessed_slice", semantic_version="1.0.0").build(
        ordered_features=["volume_ratio_20d"],
        factor_set="basic",
        factor_version="1.0.0",
        factor_generation_ids=[digest],
        label_set_name="return_5d",
        label_generation_id=digest,
        universe_snapshot_generation_id=digest,
        split_policy={
            "purge_trading_days": 1,
            "embargo_trading_days": 0,
            "splits": [
                {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-06"},
                {"name": "validation", "start_date": "2026-01-08", "end_date": "2026-01-08"},
            ],
        },
        feature_preprocessing_generation_id=None if preprocessing is None else preprocessing["generation_id"],
        input_feature_schema=input_feature_schema,
        row_count=len(output_frame),
    )


def test_cross_sectional_standardization_and_rank_are_date_bounded() -> None:
    frame = _frame()
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1",
        semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    standardized = builder.transform(frame)
    assert standardized.loc[0, "volume_ratio_20d"] == pytest.approx(-1.3416407864998738)
    assert standardized.groupby("datetime")["volume_ratio_20d"].mean().round(12).eq(0.0).all()

    rank_builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-ranked-v1",
        semantic_version="1.0.0",
        transform="rank_cross_section",
    )
    ranked = rank_builder.transform(frame)
    assert ranked.groupby("datetime")["volume_ratio_20d"].min().eq(0.0).all()
    assert ranked.groupby("datetime")["volume_ratio_20d"].max().eq(1.0).all()
    assert frame["volume_ratio_20d"].tolist() == [1.0, 2.0, 3.0, 4.0, 2.0, 4.0, 6.0, 8.0, 4.0, 1.0, 3.0, 2.0, 5.0, 6.0, 7.0, 8.0]


def test_manifest_rejects_transform_output_mismatch_and_sparse_group() -> None:
    frame = _frame()
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1",
        semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    bad_output = builder.transform(frame)
    bad_output.loc[0, "volume_ratio_20d"] += 1.0
    input_schema = _input_schema(frame)
    output_schema = _output_schema(bad_output)
    with pytest.raises(ContractError, match="preprocessing output does not match"):
        builder.build(
            frame,
            bad_output,
            input_factor_set="basic",
            input_factor_version="1.0.0",
            input_factor_generation_ids=["0" * 64],
            ordered_features=["volume_ratio_20d"],
            input_feature_schema=input_schema,
            output_feature_schema=output_schema,
        )

    sparse = frame.copy()
    sparse = sparse[sparse["datetime"] == pd.Timestamp("2026-01-08")]
    sparse_builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1",
        semantic_version="1.0.0",
        transform="standardize_cross_section",
        min_group_observations=5,
    )
    with pytest.raises(ContractError, match="minimum observations"):
        sparse_builder.transform(sparse)


def test_manifest_identity_is_stable_across_run_metadata() -> None:
    frame = _frame()
    output = _standardized(frame)
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1",
        semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    first = builder.build(
        frame,
        output,
        input_factor_set="basic",
        input_factor_version="1.0.0",
        input_factor_generation_ids=["0" * 64],
        ordered_features=["volume_ratio_20d"],
        input_feature_schema=_input_schema(frame),
        output_feature_schema=_output_schema(output),
    )
    second = dict(first)
    second["run_id"] = str(uuid.uuid4())
    second["created_at"] = datetime.now(timezone.utc).isoformat()
    second["generation_id"] = "0" * 64
    second["manifest_digest_sha256"] = "0" * 64
    second["generation_id"], second["manifest_digest_sha256"] = model_manifest_identities(
        second, schema_name="feature_preprocessing", exclude_fields={"quality_report_checksum_sha256"}
    )
    assert second["generation_id"] == first["generation_id"]
    ModelContractLoader.validate("feature_preprocessing", second)


def test_dataset_write_and_readback_bind_preprocessing(tmp_path: Path) -> None:
    frame = _frame()
    output = _standardized(frame)
    schema = _output_schema(output)
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1",
        semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    input_schema = _input_schema(frame)
    preprocessing = builder.build(
        frame,
        output,
        input_factor_set="basic",
        input_factor_version="1.0.0",
        input_factor_generation_ids=["0" * 64],
        ordered_features=["volume_ratio_20d"],
        input_feature_schema=input_schema,
        output_feature_schema=schema,
    )
    writer = DatasetWriter(tmp_path)
    partition = writer.write(
        _dataset_manifest(output, preprocessing, input_schema),
        output,
        feature_schema=schema,
        quality_report=_dataset_report(),
        preprocessing_manifest=preprocessing,
        preprocessing_input_frame=frame,
        preprocessing_frame=output,
        preprocessing_input_feature_schema=input_schema,
        preprocessing_quality_report=_report(),
    )
    assert (partition / "feature_preprocessing.json").is_file()
    loaded_manifest, loaded_frame = writer.read(
        "preprocessed_slice", "1.0.0", writer.last_published_manifest["generation_id"]
    )
    assert loaded_manifest["feature_preprocessing_generation_id"] == preprocessing["generation_id"]
    canonical_output = output.sort_values(
        ["instrument", "datetime"], kind="mergesort"
    ).reset_index(drop=True)
    pd.testing.assert_frame_equal(loaded_frame, canonical_output)


def test_dataset_read_rejects_preprocessing_manifest_tamper(tmp_path: Path) -> None:
    frame = _frame()
    output = _standardized(frame)
    schema = _output_schema(output)
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1",
        semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    input_schema = _input_schema(frame)
    preprocessing = builder.build(
        frame,
        output,
        input_factor_set="basic",
        input_factor_version="1.0.0",
        input_factor_generation_ids=["0" * 64],
        ordered_features=["volume_ratio_20d"],
        input_feature_schema=input_schema,
        output_feature_schema=schema,
    )
    writer = DatasetWriter(tmp_path)
    partition = writer.write(
        _dataset_manifest(output, preprocessing, input_schema),
        output,
        feature_schema=schema,
        quality_report=_dataset_report(),
        preprocessing_manifest=preprocessing,
        preprocessing_input_frame=frame,
        preprocessing_frame=output,
        preprocessing_input_feature_schema=input_schema,
        preprocessing_quality_report=_report(),
    )
    preprocessing_path = partition / "feature_preprocessing.json"
    document = json.loads(preprocessing_path.read_text())
    document["ordered_features"] = ["tampered_feature"]
    preprocessing_path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n")
    with pytest.raises(ContractError, match="tampered or malformed feature preprocessing"):
        writer.read("preprocessed_slice", "1.0.0", writer.last_published_manifest["generation_id"])


def test_build_rejects_infinity_in_input_or_output() -> None:
    frame = _frame()
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1", semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    with pytest.raises(ContractError, match="contains infinity"):
        bad_input = frame.copy()
        bad_input.loc[0, "volume_ratio_20d"] = float("inf")
        builder.transform(bad_input)
    output = builder.transform(frame)
    output.loc[0, "volume_ratio_20d"] = float("inf")
    with pytest.raises(ContractError, match="contains infinity"):
        builder.build(
            frame, output, input_factor_set="basic", input_factor_version="1.0.0",
            input_factor_generation_ids=["0" * 64], ordered_features=["volume_ratio_20d"],
            input_feature_schema=_input_schema(frame), output_feature_schema=_output_schema(output),
        )


def test_dataset_write_rejects_preprocessing_input_frame_mismatch(tmp_path: Path) -> None:
    frame = _frame()
    output = _standardized(frame)
    schema = _output_schema(output)
    input_schema = _input_schema(frame)
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1", semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    preprocessing = builder.build(
        frame, output, input_factor_set="basic", input_factor_version="1.0.0",
        input_factor_generation_ids=["0" * 64], ordered_features=["volume_ratio_20d"],
        input_feature_schema=input_schema, output_feature_schema=schema,
    )
    writer = DatasetWriter(tmp_path)
    with pytest.raises(ContractError, match="input frame fingerprint mismatch"):
        writer.write(
            _dataset_manifest(output, preprocessing, input_schema), output, feature_schema=schema,
            quality_report=_dataset_report(), preprocessing_manifest=preprocessing,
            preprocessing_input_frame=frame.iloc[:-1], preprocessing_frame=output,
            preprocessing_input_feature_schema=input_schema, preprocessing_quality_report=_report(),
        )


def test_dataset_write_rejects_wrong_input_feature_schema_binding(tmp_path: Path) -> None:
    frame = _frame()
    output = _standardized(frame)
    schema = _output_schema(output)
    input_schema = _input_schema(frame)
    wrong_input_schema = _input_schema(frame.assign(volume_ratio_20d=frame["volume_ratio_20d"] + 1.0))
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1", semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    preprocessing = builder.build(
        frame, output, input_factor_set="basic", input_factor_version="1.0.0",
        input_factor_generation_ids=["0" * 64], ordered_features=["volume_ratio_20d"],
        input_feature_schema=input_schema, output_feature_schema=schema,
    )
    writer = DatasetWriter(tmp_path)
    with pytest.raises(ContractError, match="input feature schema"):
        writer.write(
            _dataset_manifest(output, preprocessing, input_schema), output, feature_schema=schema,
            quality_report=_dataset_report(), preprocessing_manifest=preprocessing,
            preprocessing_input_frame=frame, preprocessing_frame=output,
            preprocessing_input_feature_schema=wrong_input_schema,
            preprocessing_quality_report=_report(),
        )


def test_dataset_write_rejects_invalid_preprocessing_manifest(tmp_path: Path) -> None:
    frame = _frame()
    output = _standardized(frame)
    schema = _output_schema(output)
    input_schema = _input_schema(frame)
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1", semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    preprocessing = builder.build(
        frame, output, input_factor_set="basic", input_factor_version="1.0.0",
        input_factor_generation_ids=["0" * 64], ordered_features=["volume_ratio_20d"],
        input_feature_schema=input_schema, output_feature_schema=schema,
    )
    del preprocessing["input_frame_sha256"]
    writer = DatasetWriter(tmp_path)
    with pytest.raises(ContractError, match=r"feature_preprocessing\.v1\.json validation failed"):
        writer.write(
            _dataset_manifest(output, preprocessing, input_schema), output, feature_schema=schema,
            quality_report=_dataset_report(), preprocessing_manifest=preprocessing,
            preprocessing_input_frame=frame, preprocessing_frame=output,
            preprocessing_input_feature_schema=input_schema, preprocessing_quality_report=_report(),
        )


def test_dataset_read_rejects_stored_input_feature_schema_tamper(tmp_path: Path) -> None:
    frame = _frame()
    output = _standardized(frame)
    schema = _output_schema(output)
    input_schema = _input_schema(frame)
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1", semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    preprocessing = builder.build(
        frame, output, input_factor_set="basic", input_factor_version="1.0.0",
        input_factor_generation_ids=["0" * 64], ordered_features=["volume_ratio_20d"],
        input_feature_schema=input_schema, output_feature_schema=schema,
    )
    writer = DatasetWriter(tmp_path)
    partition = writer.write(
        _dataset_manifest(output, preprocessing, input_schema), output, feature_schema=schema,
        quality_report=_dataset_report(), preprocessing_manifest=preprocessing,
        preprocessing_input_frame=frame, preprocessing_frame=output,
        preprocessing_input_feature_schema=input_schema, preprocessing_quality_report=_report(),
    )
    stored_schema_path = partition / "feature_schemas" / "input.json"
    document = json.loads(stored_schema_path.read_text())
    document["generation_id"] = "1" * 64
    stored_schema_path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n")
    with pytest.raises(ContractError, match="tampered or malformed feature preprocessing"):
        writer.read("preprocessed_slice", "1.0.0", writer.last_published_manifest["generation_id"])
