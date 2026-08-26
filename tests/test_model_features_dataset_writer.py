from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.accepted_store import AcceptedFactorIndexRuntime
from uq.models.dataset import DatasetBuilder
from uq.models.dataset_writer import DatasetWriter
from uq.contracts.model_layer import sha256_json
from uq.models.features import FeatureSchemaBuilder, FeatureSchemaValidator

DIGEST = "0" * 64


def _quality_report() -> dict:
    report = {
        "report_version": 1, "binding_type": "model_dataset_v1", "bound_generation_id": DIGEST,
        "policy": "reject_all", "status": "passed",
        "checks": [{"name": "row_count", "threshold": 0, "observed": 6, "level": "error", "result": "passed"}],
        "errors": [], "warnings": [], "producer_code_fingerprint": DIGEST,
    }
    report["report_checksum_sha256"] = sha256_json(report)
    return report


def _factor_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=3)
    rows = []
    for i in range(2):
        for d in dates:
            rows.append({"instrument": f"INST{i}", "datetime": d, "volume_ratio_20d": 1.0 + i * 0.1})
    return pd.DataFrame(rows)


def _publish_factor(root: Path) -> None:
    from uq.factors.store import FactorStore, factor_generation
    from uq.contracts.factor_governance import FactorRegistry
    from uq.contracts.artifacts import QualityReportStore
    from uq.contracts.canonical_v2 import file_sha256_bytes

    arguments = {
        "frame": _factor_frame(),
        "partition_date": date(2026, 1, 5),
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime.fromisoformat("2026-01-04T15:00:00+08:00"),
    }
    generation = factor_generation(**arguments)
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    if not (root / "reports").exists():
        QualityReportStore().save(root, {
            "report_version": 1, "binding_type": "factor_v1",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": [
                {"name": "null_rate", "threshold": 0.5, "observed": 0.0, "level": "error", "result": "passed"},
                {"name": "coverage", "threshold": 0.0, "observed": 1.0, "level": "error", "result": "passed"},
            ],
            "errors": [], "warnings": [],
        })
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    registry = FactorRegistry(Path(__file__).resolve().parents[1])
    FactorStore(root, registry).publish(**{k: v for k, v in arguments.items() if k != "frame"}, frame=arguments["frame"])


class TestFeatureSchemaBuilder:
    def test_build_from_factor_frame(self) -> None:
        builder = FeatureSchemaBuilder()
        schema = builder.build(_factor_frame(), source_factor_set="basic", source_factor_version="1.0.0")
        assert schema["columns"][0]["name"] == "volume_ratio_20d"
        assert len(schema["generation_id"]) == 64

    def test_validate_against_matching_frame(self) -> None:
        builder = FeatureSchemaBuilder()
        frame = _factor_frame()
        schema = builder.build(frame, source_factor_set="basic", source_factor_version="1.0.0")
        FeatureSchemaValidator.validate_against_frame(schema, frame)

    def test_validate_rejects_column_order_change(self) -> None:
        builder = FeatureSchemaBuilder()
        frame = _factor_frame()
        schema = builder.build(frame, source_factor_set="basic", source_factor_version="1.0.0")
        reordered = frame[["datetime", "volume_ratio_20d", "instrument"]]
        with pytest.raises(ContractError, match="order"):
            FeatureSchemaValidator.validate_against_frame(schema, reordered)

    def test_empty_frame_rejected(self) -> None:
        builder = FeatureSchemaBuilder()
        with pytest.raises(ContractError):
            builder.build(pd.DataFrame(), source_factor_set="basic", source_factor_version="1.0.0")


class TestDatasetWriter:
    def _build_manifest(self) -> dict:
        digest = "0" * 64
        return DatasetBuilder(dataset_name="research_slice", semantic_version="1.0.0").build(
            ordered_features=["volume_ratio_20d"],
            factor_set="basic", factor_version="1.0.0",
            factor_generation_ids=[digest],
            label_set_name="return_5d", label_generation_id=digest,
            universe_snapshot_generation_id=digest,
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-01", "end_date": "2026-01-28"}]},
            row_count=6,
        )

    def test_write_and_readback(self, tmp_path: Path) -> None:
        writer = DatasetWriter(tmp_path)
        manifest = self._build_manifest()
        frame = _factor_frame()
        schema = FeatureSchemaBuilder().build(frame, source_factor_set="basic", source_factor_version="1.0.0")
        partition = writer.write(manifest, frame, feature_schema=schema, quality_report=_quality_report())
        assert partition.is_dir()
        loaded_manifest, loaded_frame = writer.read("research_slice", "1.0.0", writer.last_published_manifest["generation_id"])
        assert list(loaded_frame.columns) == list(frame.columns)

    def test_immutable_overwrite_rejected(self, tmp_path: Path) -> None:
        writer = DatasetWriter(tmp_path)
        manifest = self._build_manifest()
        frame = _factor_frame()
        writer.write(manifest, frame, feature_schema=FeatureSchemaBuilder().build(frame, source_factor_set="basic", source_factor_version="1.0.0"), quality_report=_quality_report())
        published_manifest = writer.last_published_manifest
        with pytest.raises(ContractError, match="immutable"):
            writer.write(dict(published_manifest), _factor_frame(), feature_schema=FeatureSchemaBuilder().build(_factor_frame(), source_factor_set="basic", source_factor_version="1.0.0"))
        with pytest.raises(ContractError, match="immutable dataset already exists"):
            writer.write(manifest, _factor_frame(), feature_schema=FeatureSchemaBuilder().build(_factor_frame(), source_factor_set="basic", source_factor_version="1.0.0"))

    def test_tampered_data_rejected_on_read(self, tmp_path: Path) -> None:
        writer = DatasetWriter(tmp_path)
        manifest = self._build_manifest()
        frame = _factor_frame()
        partition = writer.write(manifest, frame, feature_schema=FeatureSchemaBuilder().build(frame, source_factor_set="basic", source_factor_version="1.0.0"), quality_report=_quality_report())
        (partition / "data.parquet").write_bytes(b"tampered")
        with pytest.raises(ContractError, match="tampered"):
            writer.read("research_slice", "1.0.0", writer.last_published_manifest["generation_id"])

    def test_end_to_end_with_accepted_store(self, tmp_path: Path) -> None:
        """Full chain: publish factor → accepted index → feature schema → dataset write."""
        _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        query = {"contract_version": 1, "filters": {}, "ordering": ["partition_date"], "visibility": "accepted_only", "pagination": {"limit": 10}}
        entries = runtime.list(query)
        assert len(entries) == 1
        gen_id = entries[0]["generation_id"]
        factor_data = runtime.read(gen_id)

        fs_builder = FeatureSchemaBuilder()
        schema = fs_builder.build(factor_data, source_factor_set="basic", source_factor_version="1.0.0")

        digest = "0" * 64
        manifest = DatasetBuilder(dataset_name="e2e_slice", semantic_version="1.0.0").build(
            ordered_features=["volume_ratio_20d"],
            factor_set="basic", factor_version="1.0.0",
            factor_generation_ids=[gen_id],
            label_set_name="return_5d", label_generation_id=digest,
            universe_snapshot_generation_id=digest,
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-01", "end_date": "2026-01-28"}]},
            row_count=len(factor_data),
        )
        writer = DatasetWriter(tmp_path)
        partition = writer.write(manifest, factor_data, feature_schema=schema, quality_report=_quality_report())
        assert partition.is_dir()
        _, loaded = writer.read("e2e_slice", "1.0.0", writer.last_published_manifest["generation_id"])
        assert len(loaded) == len(factor_data)
