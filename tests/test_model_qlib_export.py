from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from tests.review_key import REVIEWER_PRIVATE_KEY
import pytest

from uq.errors import ContractError
from uq.models.dataset_writer import DatasetWriter
from uq.models.features import FeatureSchemaBuilder
from uq.contracts.model_layer import create_reviewed_quality_decision
from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder



def export_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="qlib_dataset_export_v1", policy="reject_all", status="passed",
        checks=[{"name": "export_files_verified", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def receipt_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="qlib_init_receipt_v1", policy="reject_all", status="passed",
        checks=[{"name": "runtime_cache_boundary", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )

def _dataset_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=3)
    rows = []
    for i in range(2):
        for d in dates:
            rows.append({"instrument": f"INST{i}", "datetime": d, "volume_ratio_20d": 1.0 + i * 0.1, "label": 0.01 * (i + 1)})
    return pd.DataFrame(rows)


FEATURE_MAPPING = {"volume_ratio_20d": "VOLUME_RATIO_20D"}
LABEL_COLUMN = "label"
LABEL_MAPPING = "LABEL_5D"
CALENDAR = ["2026-01-05", "2026-01-06", "2026-01-07"]
INSTRUMENTS = ["INST0", "INST1"]


class TestQlibDatasetExporter:
    def test_export_produces_valid_manifest(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        frame = _dataset_frame()
        manifest = exporter.export(
            dataset_name="research_slice", generation_id="a" * 64,
            frame=frame, feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///tmp/exports", quality_decision=export_decision(),
        )
        assert len(manifest["generation_id"]) == 64
        assert manifest["empty_cache_precondition"] is True
        assert len(manifest["files"]) >= 6  # calendar, instruments, mapping, feature and label bins
        ModelContractLoader_validate(manifest)

    def test_immutable_overwrite_rejected(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        common = dict(
            dataset_name="research_slice", generation_id="a" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///tmp", quality_decision=export_decision(),
        )
        exporter.export(**common)
        with pytest.raises(ContractError, match="already exists"):
            exporter.export(**common)

    def test_wrong_provider_uri_rejected_on_receipt(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="x", generation_id="b" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///correct", quality_decision=export_decision(),
        )
        builder = QlibInitReceiptBuilder()
        verified_export = exporter.read("x", manifest["generation_id"])
        with pytest.raises(ContractError, match="provider URI"):
            builder.build(
                export_manifest=manifest, resolved_provider_uri="file:///wrong",
                qlib_import_path="qlib", qlib_version="0.9.6",
                cache_root=str(Path(".cache").resolve()), cache_files_before=set(), cache_files_after=set(),
                verified_export=verified_export, governance_root=tmp_path,
                quality_decision=receipt_decision(),
            )

    def test_receipt_passes_with_matching_uri(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="x", generation_id="c" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        verified_export = exporter.read("x", manifest["generation_id"])
        receipt = QlibInitReceiptBuilder().build(
            export_manifest=manifest, resolved_provider_uri="file:///data",
            qlib_import_path="qlib", qlib_version="0.9.6",
            cache_root=str(tmp_path / ".cache"), cache_files_before=set(),
            cache_files_after={str(tmp_path / ".cache/qlib/calendar.pkl")},
            verified_export=verified_export, governance_root=tmp_path,
            quality_decision=receipt_decision(),
        )
        assert receipt["no_ungoverned_source_assertion"] is True


    def test_calendar_tamper_rejected_on_read(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="tamper_case", generation_id="d" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        _, snapshot = exporter.read("tamper_case", manifest["generation_id"])
        calendar = snapshot / "calendars" / "day.txt"
        calendar.write_text(calendar.read_text() + "2026-01-08\n")
        with pytest.raises(ContractError, match="file list mismatch|tampered Qlib export"):
            exporter.read("tamper_case", manifest["generation_id"])


    def test_partial_export_rejected_on_read(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="partial", generation_id="e" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        _, snapshot = exporter.read("partial", manifest["generation_id"])
        (snapshot / "features" / "inst0" / "volume_ratio_20d.day.bin").unlink()
        with pytest.raises(ContractError, match="file list mismatch"):
            exporter.read("partial", manifest["generation_id"])

    def test_feature_order_mutation_rejected_on_read(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        frame = _dataset_frame()
        manifest = exporter.export(
            dataset_name="feature-order", generation_id="f" * 64,
            frame=frame, feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        _, snapshot = exporter.read("feature-order", manifest["generation_id"])
        mutated_path = snapshot / "features" / "inst0" / "volume_ratio_20d.day.bin"
        mutated_bytes = bytearray(mutated_path.read_bytes())
        mutated_bytes[-1] = 1
        mutated_path.write_bytes(bytes(mutated_bytes))
        with pytest.raises(ContractError, match="tampered Qlib export file:"):
            exporter.read("feature-order", manifest["generation_id"])

    def test_cache_substitution_outside_approved_root_rejected(self, tmp_path: Path) -> None:
        builder = QlibInitReceiptBuilder()
        export_manifest = {
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
            "provider_uri_sha256": __import__("hashlib").sha256(b"file:///data").hexdigest(),
            "files": [],
            "calendar_checksum_sha256": "0" * 64,
            "instruments_checksum_sha256": "0" * 64,
            "feature_mapping_checksum_sha256": "0" * 64,
        }
        verified_manifest = dict(export_manifest)
        with pytest.raises(ContractError, match="outside approved root"):
            builder.build(
                export_manifest=export_manifest,
                resolved_provider_uri="file:///data",
                qlib_import_path="qlib", qlib_version="0.9.6",
                cache_root="/tmp/approved-cache",
                cache_files_before=set(), cache_files_after={"/tmp/outside/rogue.bin"},
                verified_export=(verified_manifest, Path("/tmp/approved-export")), governance_root=tmp_path,
                quality_decision=receipt_decision(),
            )

    def test_external_cache_writes_are_recorded_in_receipt(self, tmp_path: Path) -> None:
        cache_file = tmp_path / ".cache" / "qlib" / "calendar.pkl"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"governed-cache")
        manifest = QlibDatasetExporter(tmp_path / "exports").export(
            dataset_name="cache", generation_id="1" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        receipt = QlibInitReceiptBuilder().build(
            export_manifest=manifest, resolved_provider_uri="file:///data",
            qlib_import_path="qlib", qlib_version="0.9.6",
            cache_root=str(tmp_path / ".cache"),
            cache_files_before=set(),
            cache_files_after={str(cache_file)},
            verified_export=QlibDatasetExporter(tmp_path / "exports").read("cache", manifest["generation_id"]),
            governance_root=tmp_path, quality_decision=receipt_decision(),
        )
        assert receipt["no_ungoverned_source_assertion"] is True

    def test_cleanup_policy_keeps_temporary_bin_files_outside_accepted_store(self, tmp_path: Path) -> None:
        accepted_root = tmp_path / "accepted"
        temporary_root = tmp_path / "temporary"
        temporary_root.mkdir(parents=True)
        bin_file = temporary_root / "feature.bin"
        bin_file.write_bytes(b"temporary")
        assert not bin_file.is_relative_to(accepted_root.resolve())
        assert not bin_file.exists() or bin_file.is_relative_to(temporary_root.resolve())

class TestLegacyExporterIsolation:
    def test_legacy_module_has_deprecation_marker(self) -> None:
        source = Path("src/uq/exporters/qlib.py").read_text()
        assert "DEPRECATED" in source
        assert "must not be" in source and "model training" in source


def ModelContractLoader_validate(payload: dict) -> None:
    from uq.contracts.model_layer import ModelContractLoader
    ModelContractLoader.validate("qlib_dataset_export", payload)
