import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError
from uq.factors.partition_set import build_factor_partition_set, verify_factor_partition_set
from uq.factors.raw_price import calculate_raw_price_factors
from uq.factors.store import FactorStore, factor_generation

from tests.test_release_acceptance import ROOT, local_checks, quality_document

INPUT_DATASET = "bars_daily"
INPUT_SCHEMA = "research-v1"
FACTOR_SET = "basic"
FACTOR_VERSION = "1.0.0"


def _session_frame(end: date):
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=41)
    rows = [
        {
            "instrument": "600000.XSHG",
            "datetime": value,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": float(index + 1),
            "amount": float(index + 1),
        }
        for index, value in enumerate(dates)
    ]
    factors = calculate_raw_price_factors(pd.DataFrame(rows))
    return factors[factors["datetime"] == pd.Timestamp(end)].reset_index(drop=True)


def _publish_day(root: Path, day: date):
    frame = _session_frame(day)
    arguments = {
        "factor_set": FACTOR_SET,
        "factor_version": FACTOR_VERSION,
        "frame": frame,
        "partition_date": day,
        "input_dataset": INPUT_DATASET,
        "input_schema_version": INPUT_SCHEMA,
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime.fromisoformat(f"{day.isoformat()}T08:00:00+08:00"),
    }
    generation = factor_generation(**arguments)
    QualityReportStore().save(root, {
        **quality_document(generation),
        "checks": local_checks(arguments),
    })
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    return FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def _store(root: Path) -> FactorStore:
    return FactorStore(root, FactorRegistry(ROOT))


def _build(root: Path, days: list[date]):
    return build_factor_partition_set(
        _store(root),
        input_dataset=INPUT_DATASET,
        input_schema_version=INPUT_SCHEMA,
        factor_set=FACTOR_SET,
        factor_version=FACTOR_VERSION,
        window_start=days[0],
        window_end=days[-1],
        trading_days=days,
    )


def test_factor_partition_set_binds_each_day_and_verifies(tmp_path):
    days = [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)]
    for day in days:
        _publish_day(tmp_path, day)
    document = _build(tmp_path, days)
    assert document["binding_type"] == "factor_partition_set"
    assert [entry["partition_date"] for entry in document["partitions"]] == [d.isoformat() for d in days]
    verify_factor_partition_set(_store(tmp_path), document, days)


def test_factor_partition_set_rejects_missing_trading_day(tmp_path):
    days = [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)]
    for day in days[:2]:
        _publish_day(tmp_path, day)
    with pytest.raises(ContractError):
        _build(tmp_path, days)


def test_factor_partition_set_rejects_resigned_tampered_entry(tmp_path):
    from uq.contracts.gate_contracts import sha256_json

    days = [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)]
    for day in days:
        _publish_day(tmp_path, day)
    document = _build(tmp_path, days)
    tampered = json.loads(json.dumps(document))
    tampered["partitions"][1]["data_checksum_sha256"] = "c" * 64
    tampered["set_digest_sha256"] = sha256_json(
        {key: value for key, value in tampered.items() if key != "set_digest_sha256"}
    )
    with pytest.raises(ContractError):
        verify_factor_partition_set(_store(tmp_path), tampered, days)


def test_factor_partition_set_rejects_digest_drift(tmp_path):
    days = [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)]
    for day in days:
        _publish_day(tmp_path, day)
    document = _build(tmp_path, days)
    document["window_end"] = "2026-08-20"
    with pytest.raises(ContractError, match="digest mismatch"):
        verify_factor_partition_set(_store(tmp_path), document, days)



def test_factor_partition_set_rejects_entry_outside_decision_point(tmp_path):
    from uq.factors.partition_set import _check_entry

    day = date(2026, 8, 20)
    _publish_day(tmp_path, day)
    manifest = json.loads(next(tmp_path.rglob("date=2026-08-20/manifest.json")).read_text())
    entry = {
        "partition_date": manifest["partition_date"],
        "generation_id": manifest["generation_id"],
        "manifest_digest_sha256": manifest["manifest_digest_sha256"],
        "data_checksum_sha256": manifest["data_checksum_sha256"],
        "decision_time": manifest["decision_time"],
        "run_visible_cutoff": manifest["run_visible_cutoff"],
    }
    _check_entry(manifest, entry)
    late = {**manifest, "decision_time": "2026-08-20T16:00:00+08:00", "run_visible_cutoff": "2026-08-20T16:00:00+08:00"}
    with pytest.raises(ContractError, match="decision time"):
        _check_entry(late, {**entry, "decision_time": late["decision_time"], "run_visible_cutoff": late["run_visible_cutoff"]})
