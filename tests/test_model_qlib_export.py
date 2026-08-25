from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.dataset_writer import DatasetWriter
from uq.models.features import FeatureSchemaBuilder
from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder


def _dataset_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=3)
    rows = []
    for i in range(2):
        for d in dates:
            rows.append({"instrument": f"INST{i}", "datetime": d, "volume_ratio_20d": 1.0 + i * 0.1})
    return pd.DataFrame(rows)


FEATURE_MAPPING = {"volume_ratio_20d": "VOLUME_RATIO_20D"}
CALENDAR = ["2026-01-05", "2026-01-06", "2026-01-07"]
INSTRUMENTS = ["INST0", "INST1"]


class TestQlibDatasetExporter:
    def test_export_produces_valid_manifest(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        frame = _dataset_frame()
        manifest = exporter.export(
            dataset_name="research_slice", generation_id="a" * 64,
            frame=frame, feature_mapping=FEATURE_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///tmp/exports",
        )
        assert len(manifest["generation_id"]) == 64
        assert manifest["empty_cache_precondition"] is True
        assert len(manifest["files"]) >= 4  # calendar, instruments, mapping, data
        ModelContractLoader_validate(manifest)

    def test_immutable_overwrite_rejected(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        common = dict(
            dataset_name="research_slice", generation_id="a" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///tmp",
        )
        exporter.export(**common)
        with pytest.raises(ContractError, match="already exists"):
            exporter.export(**common)

    def test_wrong_provider_uri_rejected_on_receipt(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="x", generation_id="b" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///correct",
        )
        builder = QlibInitReceiptBuilder()
        with pytest.raises(ContractError, match="provider URI"):
            builder.build(
                export_manifest=manifest, resolved_provider_uri="file:///wrong",
                qlib_import_path="qlib", qlib_version="0.9.6",
                cache_root=".cache", cache_files_before=set(), cache_files_after=set(),
            )

    def test_receipt_passes_with_matching_uri(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="x", generation_id="c" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data",
        )
        receipt = QlibInitReceiptBuilder().build(
            export_manifest=manifest, resolved_provider_uri="file:///data",
            qlib_import_path="qlib", qlib_version="0.9.6",
            cache_root=".cache", cache_files_before=set(),
            cache_files_after={".cache/qlib/calendar.pkl"},
        )
        assert receipt["no_ungoverned_source_assertion"] is True


class TestLegacyExporterIsolation:
    def test_legacy_module_has_deprecation_marker(self) -> None:
        source = Path("src/uq/exporters/qlib.py").read_text()
        assert "DEPRECATED" in source
        assert "must not be" in source and "model training" in source


def ModelContractLoader_validate(payload: dict) -> None:
    from uq.contracts.model_layer import ModelContractLoader
    ModelContractLoader.validate("qlib_dataset_export", payload)
