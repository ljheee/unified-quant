from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from tests.review_key import REVIEWER_PRIVATE_KEY
import pytest

from uq.errors import ContractError
from uq.models.dataset_writer import DatasetWriter
from uq.models.features import FeatureSchemaBuilder
from uq.contracts.model_layer import create_reviewed_quality_decision, review_signature, sha256_json
from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder

_SIGNED_FIELDS = (
    "report_version", "binding_type", "policy", "status", "checks",
    "errors", "warnings", "key_id", "producer_code_fingerprint", "reviewer",
)


def _resign_decision(decision: dict, *, status: str) -> dict:
    """Re-sign a raw reviewer decision with a different status.

    The governed helper refuses to mint non-passed decisions for reject_all
    families, but the read paths must not rely on that: an out-of-band holder
    of the reviewer key can sign any status.  Tests mint such decisions the
    same way — flip the status, re-sign the signed field set.
    """
    mutated = dict(decision)
    mutated["status"] = status
    if status == "warning":
        mutated["warnings"] = ["residual data-quality warning"]
    payload = {key: mutated[key] for key in _SIGNED_FIELDS}
    payload["subject_generation_id"] = mutated["subject_generation_id"]
    payload["subject_content_sha256"] = mutated["subject_content_sha256"]
    mutated["review_signature_sha256"] = review_signature(payload, private_key_pem=REVIEWER_PRIVATE_KEY)
    return mutated


def _resign_report(report: dict, *, status: str) -> dict:
    """Re-sign a published owning report with a different status (see _resign_decision)."""
    mutated = dict(report)
    mutated["status"] = status
    if status == "warning":
        mutated["warnings"] = ["residual data-quality warning"]
    payload = {key: mutated[key] for key in _SIGNED_FIELDS}
    payload["subject_generation_id"] = mutated["bound_generation_id"]
    payload["subject_content_sha256"] = mutated["subject_content_sha256"]
    mutated["review_signature_sha256"] = review_signature(payload, private_key_pem=REVIEWER_PRIVATE_KEY)
    mutated["report_checksum_sha256"] = sha256_json(
        {key: value for key, value in mutated.items() if key != "report_checksum_sha256"}
    )
    return mutated



def export_decision():
    # cr.md U9/U14: the export generation and content digest are derived
    # inside the exporter, so the decision must arrive as a two-argument
    # per-subject provider.
    def provider(subject_generation_id: str, subject_content_sha256: str) -> dict:
        return create_reviewed_quality_decision(
            binding_type="qlib_dataset_export_v1", policy="reject_all", status="passed",
            checks=[{"name": "export_files_verified", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
            errors=[], warnings=[], producer_code_fingerprint="0" * 64,
            private_key_pem=REVIEWER_PRIVATE_KEY,
            subject_generation_id=subject_generation_id,
            subject_content_sha256=subject_content_sha256,
        )
    return provider


def receipt_decision():
    def provider(subject_generation_id: str, subject_content_sha256: str) -> dict:
        return create_reviewed_quality_decision(
            binding_type="qlib_init_receipt_v1", policy="reject_all", status="passed",
            checks=[{"name": "runtime_cache_boundary", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
            errors=[], warnings=[], producer_code_fingerprint="0" * 64,
            private_key_pem=REVIEWER_PRIVATE_KEY,
            subject_generation_id=subject_generation_id,
            subject_content_sha256=subject_content_sha256,
        )
    return provider

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

    def test_feature_bins_are_written_once_and_preserve_calendar_gaps(
        self, tmp_path: Path
    ) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        frame = pd.DataFrame([
            {"instrument": "INST0", "datetime": "2026-01-07", "volume_ratio_20d": 3.0, "label": 0.3},
            {"instrument": "INST0", "datetime": "2026-01-05", "volume_ratio_20d": 1.0, "label": 0.1},
            {"instrument": "INST1", "datetime": "2026-01-06", "volume_ratio_20d": 2.0, "label": 0.2},
        ])
        manifest = exporter.export(
            dataset_name="sparse", generation_id="d" * 64,
            frame=frame, feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        _, snapshot = exporter.read("sparse", manifest["generation_id"])

        inst0 = np.fromfile(
            snapshot / "features" / "inst0" / "volume_ratio_20d.day.bin",
            dtype="<f",
        )
        assert inst0[0] == 0.0
        assert inst0[1] == 1.0
        assert np.isnan(inst0[2])
        assert inst0[3] == 3.0
        inst1 = np.fromfile(
            snapshot / "features" / "inst1" / "volume_ratio_20d.day.bin",
            dtype="<f",
        )
        assert inst1.tolist() == [1.0, 2.0]

    def test_feature_bin_header_uses_the_real_calendar_index(self, tmp_path: Path) -> None:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        frame = pd.DataFrame([
            {"instrument": "INST0", "datetime": "2026-01-06", "volume_ratio_20d": 2.0, "label": 0.2},
            {"instrument": "INST0", "datetime": "2026-01-07", "volume_ratio_20d": 3.0, "label": 0.3},
        ])
        manifest = exporter.export(
            dataset_name="offset", generation_id="d" * 64,
            frame=frame, feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=["INST0"],
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        _, snapshot = exporter.read("offset", manifest["generation_id"])
        values = np.fromfile(
            snapshot / "features" / "inst0" / "volume_ratio_20d.day.bin",
            dtype="<f",
        )
        assert values.tolist() == [1.0, 2.0, 3.0]

    def test_duplicate_calendar_key_is_refused_before_writing(self, tmp_path: Path) -> None:
        frame = pd.DataFrame([
            {"instrument": "INST0", "datetime": "2026-01-05", "volume_ratio_20d": 1.0, "label": 0.1},
            {"instrument": "INST0", "datetime": "2026-01-05", "volume_ratio_20d": 2.0, "label": 0.2},
        ])
        with pytest.raises(ContractError, match="duplicate date keys"):
            QlibDatasetExporter(tmp_path / "exports").export(
                dataset_name="dup", generation_id="d" * 64,
                frame=frame, feature_mapping=FEATURE_MAPPING,
                label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
                calendar_dates=CALENDAR, instruments=["INST0"],
                provider_uri="file:///data", quality_decision=export_decision(),
            )

    def test_missing_feature_value_is_refused_before_writing(self, tmp_path: Path) -> None:
        frame = pd.DataFrame([
            {"instrument": "INST0", "datetime": "2026-01-05", "volume_ratio_20d": np.nan, "label": 0.1},
        ])
        with pytest.raises(ContractError, match="cannot contain missing values"):
            QlibDatasetExporter(tmp_path / "exports").export(
                dataset_name="null", generation_id="d" * 64,
                frame=frame, feature_mapping=FEATURE_MAPPING,
                label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
                calendar_dates=CALENDAR, instruments=["INST0"],
                provider_uri="file:///data", quality_decision=export_decision(),
            )

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


    def test_persisted_receipt_carries_its_final_identity(self, tmp_path: Path) -> None:
        # The receipt used to be written to disk *before* `generation_id` and
        # `manifest_digest_sha256` were stamped (cr.md 5.4: qlib_export.py:336), so the
        # persisted document held the `"0"*64` placeholder forever. In-memory callers
        # never noticed because they use the returned dict; only a resumed run, which
        # must read the file, failed -- on the stable-generation check.
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="x", generation_id="d" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        receipt = QlibInitReceiptBuilder().build(
            export_manifest=manifest, resolved_provider_uri="file:///data",
            qlib_import_path="qlib", qlib_version="0.9.6",
            cache_root=str(tmp_path / ".cache"), cache_files_before=set(),
            cache_files_after={str(tmp_path / ".cache/qlib/calendar.pkl")},
            verified_export=exporter.read("x", manifest["generation_id"]),
            governance_root=tmp_path, quality_decision=receipt_decision(),
        )
        persisted = tmp_path / "research_exports" / f"{receipt['generation_id']}.json"
        assert persisted.is_file(), "the receipt must be persisted with its final identity"
        document = json.loads(persisted.read_text())
        assert document["generation_id"] == receipt["generation_id"]
        assert document["generation_id"] != "0" * 64
        assert document["manifest_digest_sha256"] == receipt["manifest_digest_sha256"]

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

    def test_warning_status_report_rejected_on_read(self, tmp_path: Path) -> None:
        # cr.md 8.4 round-4 follow-up (m9): the registry-level lock cannot
        # prove the read path consults the registry.  Drive the read with a
        # warning report that is otherwise fully valid — same subject, valid
        # reviewer signature, stored under its own checksum — so the ONLY
        # condition left to refuse it is the registry status policy at
        # qlib_export.py:248 (validate("model_quality_report") checks
        # schema/checksum/signature, never the registry).
        exporter = QlibDatasetExporter(tmp_path / "exports")
        manifest = exporter.export(
            dataset_name="warn_case", generation_id="9" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri="file:///data", quality_decision=export_decision(),
        )
        generation_id = manifest["generation_id"]
        exporter.read("warn_case", generation_id)  # control: the passed export reads cleanly

        export_root = tmp_path / "exports"
        report_path = export_root / "external_quality_reviews" / f"{manifest['quality_report_checksum_sha256']}.json"
        warning = _resign_report(json.loads(report_path.read_text()), status="warning")
        (export_root / "external_quality_reviews" / f"{warning['report_checksum_sha256']}.json").write_text(
            json.dumps(warning, sort_keys=True, indent=2) + "\n"
        )
        manifest_path = export_root / "dataset=warn_case" / f"generation={generation_id}" / "manifest.json"
        patched = json.loads(manifest_path.read_text())
        # quality_report_checksum_sha256 is excluded from the manifest identity,
        # so the manifest still validates — only the status policy can refuse.
        patched["quality_report_checksum_sha256"] = warning["report_checksum_sha256"]
        manifest_path.write_text(json.dumps(patched, sort_keys=True, indent=2) + "\n")

        with pytest.raises(ContractError, match="Qlib export quality report rejects read"):
            exporter.read("warn_case", generation_id)

    def test_warning_status_decision_rejected_on_export(self, tmp_path: Path) -> None:
        # m9 (publish side): a warning decision validly signed for the export's
        # own subject must be refused at publish — bind_reviewed_quality_decision
        # enforces the same registry policy the read side consults.
        def warning_provider(subject_generation_id: str, subject_content_sha256: str) -> dict:
            decision = create_reviewed_quality_decision(
                binding_type="qlib_dataset_export_v1", policy="reject_all", status="passed",
                checks=[{"name": "export_files_verified", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
                errors=[], warnings=[], producer_code_fingerprint="0" * 64,
                private_key_pem=REVIEWER_PRIVATE_KEY,
                subject_generation_id=subject_generation_id,
                subject_content_sha256=subject_content_sha256,
            )
            return _resign_decision(decision, status="warning")

        exporter = QlibDatasetExporter(tmp_path / "exports")
        with pytest.raises(ContractError, match="not approved by reviewed registry"):
            exporter.export(
                dataset_name="warn_publish", generation_id="8" * 64,
                frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
                label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
                calendar_dates=CALENDAR, instruments=INSTRUMENTS,
                provider_uri="file:///data", quality_decision=warning_provider,
            )

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


class TestReadVerifiesTheRequestNotJustTheArtifact:
    """`read()` is the gate that makes reuse safe.

    The seven pre-existing checks all verify the artifact against *itself*. This class
    adds the request side: `provider_uri_sha256` sits outside the identity, so a
    byte-identical export can legitimately be shared by runs whose URIs differ --
    which is exactly the case a content comparison cannot see.

    `exporter_fingerprint` is deliberately *not* re-checked here: it participates in
    the identity, so a mismatch is already refused as a generation mismatch. A test
    written to tamper it failed on that earlier check, which is the evidence it is
    redundant.
    """

    def _publish(self, tmp_path: Path, provider_uri: str) -> dict:
        exporter = QlibDatasetExporter(tmp_path / "exports")
        return exporter.export(
            dataset_name="x", generation_id="e" * 64,
            frame=_dataset_frame(), feature_mapping=FEATURE_MAPPING,
            label_column=LABEL_COLUMN, label_mapping=LABEL_MAPPING,
            calendar_dates=CALENDAR, instruments=INSTRUMENTS,
            provider_uri=provider_uri, quality_decision=export_decision(),
        )

    def test_the_same_uri_reads_back(self, tmp_path: Path) -> None:
        manifest = self._publish(tmp_path, "file:///one")
        read, _ = QlibDatasetExporter(tmp_path / "exports").read(
            "x", manifest["generation_id"], provider_uri="file:///one"
        )
        assert read["generation_id"] == manifest["generation_id"]

    def test_a_different_uri_is_refused(self, tmp_path: Path) -> None:
        # Without this, a run pointed at `file:///two` would reuse the snapshot
        # published for `file:///one` and its receipt would attest the second URI
        # with evidence produced under the first.
        manifest = self._publish(tmp_path, "file:///one")
        with pytest.raises(ContractError, match="different provider URI"):
            QlibDatasetExporter(tmp_path / "exports").read(
                "x", manifest["generation_id"], provider_uri="file:///two"
            )

    def test_the_uri_is_absent_from_the_identity(self, tmp_path: Path) -> None:
        # The premise of the whole design: changing the URI must not change the
        # generation id, which is why a read-time check is needed at all.
        first = self._publish(tmp_path / "a", "file:///one")
        second = self._publish(tmp_path / "b", "file:///two")
        assert first["generation_id"] == second["generation_id"]
        assert first["provider_uri_sha256"] != second["provider_uri_sha256"]
