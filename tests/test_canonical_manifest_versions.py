"""Canonical partition manifests are read through an explicit version allowlist.

Two canonical writers exist. ``uq.store.pit_store.CanonicalStore`` (the ingest
pipeline, and the shape the real stores on disk carry) publishes
``manifest_version: 1``; ``uq.contracts.canonical_v2.CanonicalV2Store`` publishes
``manifest_version: 2`` with ``package_provenance`` and a run-local manifest
digest.  ``ManifestFirstReader`` used to validate every document against the v1
contract, so a v2 partition was invisible to the read path every layer uses
while an unversioned document silently took the v1 path (cr.md R2).

The reader now reads the version first and validates it against that version's
own contract and identities, failing closed on anything outside the allowlist.
Note the two reads keep different strictness on purpose: this one proves the
manifest identity and the data integrity, while
``uq.contracts.canonical_v2.read_canonical_v2`` additionally requires an
out-of-band trust anchor and the bound quality report.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tests.test_publish_factor_partitions import V2_SCHEMA, bars, build_store
from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.store.reader import ManifestFirstReader

DAY = date(2026, 3, 2)


def publish_v2(root: Path):
    """Publish one canonical-v2 partition, quality report included."""
    schema = load_schema(V2_SCHEMA)
    frame = bars(DAY)
    store = CanonicalV2Store(root)
    generation = store.prepare_generation(schema, DAY, frame, {}, {})
    report_dir = QualityReportStore().save(root, {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                    "level": "error", "result": "passed"}],
        "errors": [], "warnings": [],
    })
    store.publish(schema, DAY, frame, {}, {},
                  quality_checksum=file_sha256_bytes((report_dir / "report.json").read_bytes()))
    return schema, frame


def manifest_path(root: Path, schema) -> Path:
    return (root / "canonical" / schema.dataset / schema.version
            / f"date={DAY.isoformat()}" / "manifest.json")


def rewrite_manifest(path: Path, **changes) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return document


def test_v1_partitions_still_read_after_the_version_dispatch(tmp_path):
    schema = load_schema(V2_SCHEMA)
    build_store(tmp_path, [DAY])
    manifest = json.loads(manifest_path(tmp_path, schema).read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert len(ManifestFirstReader(tmp_path).read(schema, DAY)) == len(bars(DAY))


def test_v2_partition_reads_through_the_governed_reader(tmp_path):
    schema, frame = publish_v2(tmp_path)
    read = ManifestFirstReader(tmp_path).read(schema, DAY)
    assert list(read.columns) == list(frame.columns)
    assert len(read) == len(frame)
    assert sorted(read["instrument"]) == sorted(frame["instrument"])


@pytest.mark.parametrize("change", [{"manifest_version": 3}, {"manifest_version": None}])
def test_manifest_version_outside_the_allowlist_fails_closed(tmp_path, change):
    schema, _ = publish_v2(tmp_path)
    rewrite_manifest(manifest_path(tmp_path, schema), **change)
    with pytest.raises(ContractError, match="unsupported canonical manifest_version"):
        ManifestFirstReader(tmp_path).read(schema, DAY)


def test_tampered_v2_manifest_identity_fails_closed(tmp_path):
    schema, _ = publish_v2(tmp_path)
    rewrite_manifest(manifest_path(tmp_path, schema), row_count=999)
    with pytest.raises(ContractError, match="canonical-v2 generation digest mismatch"):
        ManifestFirstReader(tmp_path).read(schema, DAY)
