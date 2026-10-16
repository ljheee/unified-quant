"""The label-set publisher (the last pre-model family with no production writer).

`LabelBuilder` computes the manifest and `LabelStore` reads both halves back, but
nothing could write one, so the dataset stage of a research run had no way to
produce its own input. These locks cover the properties that make a published
label set trustworthy rather than merely present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.label_store_writer import LabelSetWriter
from uq.models.labels import LabelBuilder
from uq.research_chain.owning_contracts import LabelStore

_DATES = pd.bdate_range("2026-01-05", periods=10)


def _adjusted_frame():
    rows = []
    for index in range(2):
        for day in _DATES:
            rows.append({
                "instrument": f"INST{index:04d}", "datetime": day,
                "close": 10.0 + index, "adj_factor": 1.0 + 0.01 * index,
                "limit_up": False, "limit_down": False, "delisted": False, "suspended": False,
                "listing_date": pd.Timestamp("2020-01-01", tz="UTC"),
            })
    return pd.DataFrame(rows)


def _binding(frame: pd.DataFrame) -> dict:
    from uq.contracts.model_layer import sha256_json
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort")
    checksum = sha256_json({"rows": [
        [str(row[0]), pd.Timestamp(row[1]).isoformat(), float(row[2]), float(row[3]), bool(row[4]), str(pd.Timestamp(row[5]).date())]
        for row in ordered[["instrument", "datetime", "close", "adj_factor", "suspended", "listing_date"]].itertuples(index=False)
    ]})
    return {
        "binding": "adjusted_price", "dataset": "bars_adjusted", "schema_version": "adjusted-v1",
        "partition_date": "2026-01-15", "generation_id": "0" * 64,
        "data_checksum_sha256": checksum, "visible_cutoff": "2026-01-15T15:00:00+08:00",
    }


def _built():
    # The writer must be handed the frame the builder actually described, so take
    # both halves from the builder rather than fabricating a frame that no manifest
    # accounts for.
    frame = _adjusted_frame()
    return LabelBuilder(name="return_5d", semantic_version="1.0.0").build_with_frame(
        frame, upstream_bindings=[_binding(frame)]
    )


def test_published_label_set_reads_back_through_the_governed_store(tmp_path):
    manifest, frame = _built()
    identity = LabelSetWriter(tmp_path).publish(manifest, frame)
    assert identity["rows"] == len(frame)
    stored_manifest, stored = LabelStore(tmp_path).read_frame(identity["generation_id"])
    assert stored_manifest["generation_id"] == identity["generation_id"]
    assert list(stored.columns) == list(frame.columns)
    assert len(stored) == len(frame)


def test_the_published_manifest_is_a_valid_label_set_contract(tmp_path):
    manifest, frame = _built()
    identity = LabelSetWriter(tmp_path).publish(manifest, frame)
    document = json.loads((Path(identity["partition"]) / "manifest.json").read_text())
    # the contract is closed, so a stray field would have failed at write time
    assert document["generation_id"] == identity["generation_id"]
    assert document["manifest_digest_sha256"] == identity["manifest_digest_sha256"]
    assert document["row_count"] == len(frame)


def test_republishing_the_same_label_set_is_refused(tmp_path):
    manifest, frame = _built()
    writer = LabelSetWriter(tmp_path)
    writer.publish(manifest, frame)
    with pytest.raises(ContractError, match="already published"):
        writer.publish(manifest, frame)


def test_a_frame_that_disagrees_with_the_manifest_is_refused(tmp_path):
    manifest, frame = _built()
    # the builder computed its checksums over the frame it was given; a different
    # frame must not be quietly reconciled
    other = frame.copy()
    other["label"] = 0.5
    with pytest.raises(ContractError, match="does not match the manifest"):
        LabelSetWriter(tmp_path).publish(manifest, other)


def test_an_empty_label_set_is_refused(tmp_path):
    manifest, frame = _built()
    with pytest.raises(ContractError, match="empty label set"):
        LabelSetWriter(tmp_path).publish(manifest, frame.iloc[:0])


def test_duplicate_label_keys_are_refused(tmp_path):
    manifest, frame = _built()
    duplicated = pd.concat([frame, frame.head(2)], ignore_index=True)
    with pytest.raises(ContractError, match="duplicate instrument/decision_date"):
        LabelSetWriter(tmp_path).publish(manifest, duplicated)


def _report(generation_id, status="passed", binding_type="label_set_v1"):
    from uq.contracts.model_layer import sha256_json
    report = {
        "report_version": 1, "binding_type": binding_type,
        "bound_generation_id": generation_id, "policy": "reject_all",
        "status": status,
        "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                    "level": "error", "result": "passed"}],
        "errors": [], "warnings": [],
        "producer_code_fingerprint": "0" * 64, "report_checksum_sha256": "",
    }
    report["report_checksum_sha256"] = sha256_json(
        {key: value for key, value in report.items() if key != "report_checksum_sha256"}
    )
    return report


def test_an_unreviewed_quality_report_is_refused(tmp_path):
    manifest, frame = _built()
    # a provider: the caller cannot know the generation id before it is derived
    provider = lambda generation_id: _report(generation_id, status="warning")
    with pytest.raises(ContractError, match="not reviewed-accepted"):
        LabelSetWriter(tmp_path).publish(manifest, frame, quality_report=provider)


def test_an_accepted_quality_report_is_resolved_against_the_subject(tmp_path):
    # label_set.v1 is additionalProperties:false with no quality field, so a report
    # cannot be stored in the manifest; it is resolved against this generation and
    # its status echoed into the identity.
    manifest, frame = _built()
    provider = lambda generation_id: _report(generation_id, status="passed")
    identity = LabelSetWriter(tmp_path).publish(manifest, frame, quality_report=provider)
    assert identity["quality_report_status"] == "passed"
    document = json.loads((Path(identity["partition"]) / "manifest.json").read_text())
    assert "quality_report_checksum_sha256" not in document


def test_a_report_bound_to_another_subject_is_refused(tmp_path):
    manifest, frame = _built()
    with pytest.raises(ContractError, match="bound to a different subject"):
        LabelSetWriter(tmp_path).publish(
            manifest, frame, quality_report=_report("f" * 64, status="passed")
        )


def test_a_report_of_another_family_is_refused(tmp_path):
    manifest, frame = _built()
    provider = lambda generation_id: _report(generation_id, binding_type="model_dataset_v1")
    with pytest.raises(ContractError, match="is not label_set_v1"):
        LabelSetWriter(tmp_path).publish(manifest, frame, quality_report=provider)


def test_a_tampered_published_frame_is_detected_by_the_reader(tmp_path):
    manifest, frame = _built()
    identity = LabelSetWriter(tmp_path).publish(manifest, frame)
    data = Path(identity["partition"]) / "data.parquet"
    tampered = pd.read_parquet(data)
    tampered.loc[0, "label"] = 123.0
    tampered.to_parquet(data, index=False)
    with pytest.raises(ContractError):
        LabelStore(tmp_path).read_frame(identity["generation_id"])


def test_publishing_repeatedly_does_not_leak_file_descriptors(tmp_path):
    # cr.md 8.34: `_fsync_tree` opened the store root and never closed it, so a
    # process that published in a loop leaked one descriptor per call and died at
    # EMFILE (~60 publishes). Publishing 20 sets must not grow the fd table.
    import os

    def open_fds() -> int:
        return len(os.listdir("/dev/fd"))

    before = open_fds()
    for index in range(20):
        manifest, frame = _built()
        LabelSetWriter(tmp_path / f"run{index}").publish(manifest, frame)
    assert open_fds() == before


def test_a_manifest_that_misdescribes_dtypes_is_refused(tmp_path):
    # cr.md 8.34: the read-back reconcile compared the restored frame against the
    # *in-memory* frame instead of the manifest, so a manifest declaring a dtype the
    # partition does not carry published cleanly -- and only failed later, at the
    # governed reader. The writer now applies the reader's own normalization, so the
    # two boundaries cannot disagree about the same artifact.
    manifest, frame = _built()
    lying = {**manifest, "dtypes": {**manifest["dtypes"], "label": "int64"}}
    with pytest.raises(ContractError, match="read-back dtype mismatch"):
        LabelSetWriter(tmp_path).publish(lying, frame)


def test_a_frame_missing_a_key_column_is_a_contract_error(tmp_path):
    manifest, frame = _built()
    with pytest.raises(ContractError, match="missing columns"):
        LabelSetWriter(tmp_path).publish(manifest, frame.drop(columns=["decision_date"]))
