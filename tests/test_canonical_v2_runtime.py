import json
import shutil
import tempfile
from pathlib import Path
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from uq.contracts.canonical_v2 import (
    CanonicalMigrationLedger,
    CanonicalV2Store,
    read_canonical_v2,
    sha256_bytes,
)
from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.schema import load_schema
from uq.errors import ContractError


def bars():
    return pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.2], "low": [9.8], "close": [10.0],
        "volume": [10000.0], "amount": [100000.0],
    })


def anchor(root, schema=None, day=date(2026, 8, 21)):
    manifest = json.loads(
        (root / "canonical" / (schema or load_schema("config/schemas/bars_daily.research-v1.yaml")).dataset / (schema or load_schema("config/schemas/bars_daily.research-v1.yaml")).version / f"date={day.isoformat()}" / "manifest.json").read_text()
    )
    return manifest["trust_anchor_sha256"]



@pytest.fixture
def schema():
    return load_schema("config/schemas/bars_daily.research-v1.yaml")


def canonical_report(generation):
    return {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [{
            "name": "coverage", "threshold": 0, "observed": 0,
            "level": "error", "result": "passed",
        }],
        "errors": [],
        "warnings": [],
    }


def publish_with_quality(store, day, *, schema=None):
    if schema is None:
        schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    generation = store.prepare_generation(schema, day, bars(), {}, {})
    directory = QualityReportStore().save(store.root, canonical_report(generation))
    checksum = file_sha256_bytes((directory / "report.json").read_bytes())
    return store.publish(schema, day, bars(), {}, {}, quality_checksum=checksum)


def publish(tmp_path):
    store = CanonicalV2Store(tmp_path)
    publish_with_quality(store, date(2026, 8, 21))
    return store


def test_generation_excludes_run_metadata(schema):

    roots = [Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())]
    for root in roots:
        publish_with_quality(CanonicalV2Store(root), date(2026, 8, 21), schema=schema)
    left = json.loads((roots[0] / "canonical/bars_daily/research-v1/date=2026-08-21/manifest.json").read_text())
    right = json.loads((roots[1] / "canonical/bars_daily/research-v1/date=2026-08-21/manifest.json").read_text())
    assert left["generation_id"] == right["generation_id"]
    assert left["run_id"] == right["run_id"]
    assert left["created_at"] == right["created_at"]
    assert left["manifest_digest_sha256"] == right["manifest_digest_sha256"]


def test_reader_requires_external_anchor_and_path_identity(tmp_path, schema):
    publish(tmp_path)
    good_anchor = anchor(tmp_path)
    frame = read_canonical_v2(tmp_path, schema, date(2026, 8, 21), expected_anchor=good_anchor)
    assert len(frame) == 1

    with pytest.raises(ContractError, match="trust anchor"):
        read_canonical_v2(tmp_path, schema, date(2026, 8, 21), expected_anchor="f" * 64)

    copied = tmp_path / "canonical/bars_daily/research-v1/date=2026-08-22"
    copied.mkdir(parents=True)
    for name in ("data.parquet", "manifest.json"):
        (copied / name).write_bytes((tmp_path / "canonical/bars_daily/research-v1/date=2026-08-21" / name).read_bytes())
    with pytest.raises(ContractError, match="physical path"):
        read_canonical_v2(tmp_path, schema, date(2026, 8, 22), expected_anchor=good_anchor)


def test_canonical_publication_requires_bound_quality_report(tmp_path, schema):
    store = CanonicalV2Store(tmp_path)
    generation = store.prepare_generation(schema, date(2026, 8, 21), bars(), {}, {})
    checksum = "0" * 64
    with pytest.raises(ContractError, match="missing quality report"):
        store.publish(schema, date(2026, 8, 21), bars(), {}, {}, quality_checksum=checksum)
    assert not (tmp_path / "canonical" / schema.dataset / schema.version / "date=2026-08-21").exists()
    assert generation


def test_canonical_publication_rejects_wrong_binding_and_failed_report(tmp_path, schema):
    day = date(2026, 8, 21)
    store = CanonicalV2Store(tmp_path)
    generation = store.prepare_generation(schema, day, bars(), {}, {})

    wrong_generation = QualityReportStore().save(
        tmp_path, {**canonical_report("f" * 64), "bound_generation_id": "e" * 64}
    )
    with pytest.raises(ContractError, match="quality report"):
        store.publish(schema, day, bars(), {}, {}, quality_checksum=file_sha256_bytes((wrong_generation / "report.json").read_bytes()))

    failed = QualityReportStore().save(
        tmp_path,
        {
            **canonical_report(generation),
            "status": "rejected",
            "checks": [{**canonical_report(generation)["checks"][0], "result": "failed"}],
            "errors": ["coverage failed"],
        },
    )
    with pytest.raises(ContractError, match="canonical-v2 quality report rejects publication"):
        store.publish(schema, day, bars(), {}, {}, quality_checksum=file_sha256_bytes((failed / "report.json").read_bytes()))
    assert not (tmp_path / "canonical" / schema.dataset / schema.version / f"date={day.isoformat()}").exists()


def test_canonical_publication_rejects_tampered_report(tmp_path, schema):
    day = date(2026, 8, 21)
    store = CanonicalV2Store(tmp_path)
    generation = store.prepare_generation(schema, day, bars(), {}, {})
    directory = QualityReportStore().save(tmp_path, canonical_report(generation))
    path = directory / "report.json"
    original = path.read_bytes()
    path.write_bytes(original + b"tamper")
    with pytest.raises(ContractError, match="tampered quality report bytes"):
        store.publish(schema, day, bars(), {}, {}, quality_checksum=file_sha256_bytes(original))
    path.write_bytes(original)
    checksum = file_sha256_bytes(original)
    partition = store.publish(schema, day, bars(), {}, {}, quality_checksum=checksum)
    assert partition.exists()


def test_canonical_read_rejects_missing_tampered_or_misbound_report(tmp_path, schema):
    publish(tmp_path)
    manifest_path = tmp_path / "canonical/bars_daily/research-v1/date=2026-08-21/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    anchor = manifest["trust_anchor_sha256"]
    report_root = tmp_path / "reports/canonical_v2" / manifest["generation_id"]

    shutil.rmtree(report_root)
    with pytest.raises(ContractError, match="missing quality report"):
        read_canonical_v2(tmp_path, schema, date(2026, 8, 21), expected_anchor=anchor)
    directory = QualityReportStore().save(tmp_path, canonical_report(manifest["generation_id"]))
    report_path = directory / "report.json"

    original = report_path.read_bytes()
    report_path.write_bytes(original.replace(b'"coverage"', b'"coverage_x"'))
    with pytest.raises(ContractError, match="tampered quality report bytes"):
        read_canonical_v2(tmp_path, schema, date(2026, 8, 21), expected_anchor=anchor)

    report_path.write_bytes(original)
    directory.rename(tmp_path / "reports/canonical_v2/other")
    with pytest.raises(ContractError, match="missing quality report"):
        read_canonical_v2(tmp_path, schema, date(2026, 8, 21), expected_anchor=anchor)


def test_rejects_invalid_uuid_datetime_date_dtype_map(schema):
    publish(tmp_path := Path(tempfile.mkdtemp()))
    partition = tmp_path / "canonical/bars_daily/research-v1/date=2026-08-21"
    manifest_path = partition / "manifest.json"

    cases = [
        {"run_id": "not-a-uuid"},
        {"created_at": "not-a-datetime"},
        {"partition_date": "2026-02-31"},
        {"dtypes": {}},
    ]
    for patch in cases:
        manifest = json.loads(manifest_path.read_text())
        manifest.update(patch)
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(ContractError):
            read_canonical_v2(tmp_path, schema, date(2026, 8, 21), expected_anchor=anchor(tmp_path))


def migration_record(source, target_content="e" * 64, target_manifest="f" * 64):
    return {
        **source,
        "target_dataset": "bars_daily", "target_schema_version": "v2",
        "target_partition_path": "canonical/bars_daily/v2/date=2026-08-21",
        "target_content_generation_id": target_content,
        "target_manifest_digest_sha256": target_manifest,
    }


def base_source():
    return {
        "migration_version": 1, "action": "read_only_legacy",
        "source_dataset": "bars_daily", "source_schema_version": "v1",
        "source_partition_path": "canonical/bars_daily/v1/date=2026-08-21",
        "source_partition_date": "2026-08-21",
        "source_data_checksum_sha256": "a" * 64,
        "source_schema_checksum_sha256": "b" * 64,
        "source_legacy_generation_id": "c" * 64,
        "source_manifest_digest_sha256": "d" * 64,
        "migration_algorithm_version": "canonical-migration.v1",
        "decision_time": "2026-08-23T00:00:00Z",
        "run_visible_cutoff": "2026-08-23T01:00:00Z",
        "operator": "reviewer", "approval_reference": "gate-plan-v0.9",
    }


def test_migration_ledger_append_only_negative_paths():
    ledger = CanonicalMigrationLedger(Path(tempfile.mkdtemp()))
    record = migration_record(base_source())
    result = ledger.append(record)
    assert result.mapping_checksum_sha256
    assert len(result.audit_path.read_text().splitlines()) == 1

    with pytest.raises(ContractError, match="duplicate canonical migration source mapping"):
        ledger.append(migration_record(base_source()))

    with pytest.raises(ContractError, match="duplicate canonical migration source mapping"):
        ledger.append(migration_record({**base_source(), "action": "republish_v2"}))

    republish = migration_record({
        **base_source(),
        "action": "republish_v2",
        "source_partition_path": "canonical/bars_daily/v1/date=2026-08-22",
        "source_partition_date": "2026-08-22",
    })
    ledger.append(republish)
    with pytest.raises(ContractError, match="duplicate canonical migration source mapping"):
        ledger.append(republish)
    with pytest.raises(ContractError, match="reused canonical migration destination"):
        ledger.append(migration_record({
            **base_source(),
            "action": "republish_v2",
            "source_partition_path": "canonical/bars_daily/v1/date=2026-08-23",
            "source_partition_date": "2026-08-23",
        }))

    original = ledger.audit_path.read_text()
    ledger.audit_path.write_text(original.replace("reviewer", "attacker"))
    with pytest.raises(ContractError, match="tampered canonical migration"):
        ledger.records()
    assert ledger.audit_path.read_text() == original.replace("reviewer", "attacker")


def test_unapproved_action_is_rejected():
    source = base_source()
    source.pop("approval_reference")
    with pytest.raises(Exception):
        CanonicalMigrationLedger(Path(tempfile.mkdtemp())).append(
            migration_record(source)
        )
