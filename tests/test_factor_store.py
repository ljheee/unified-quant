import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError
from uq.factors.raw_price import calculate_raw_price_factors
from uq.factors.store import FactorStore, _validate_factor_frame, factor_generation, read_factor_partition


ROOT = Path(__file__).resolve().parents[1]


def frame():
    rows = [
        {
            "instrument": "600000.XSHG",
            "datetime": pd.Timestamp(2026, 7, day),
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": float(day),
            "amount": float(day),
        }
        for day in range(1, 22)
    ]
    warmup = [
        {
            "instrument": "600000.XSHG",
            "datetime": pd.Timestamp(2026, 6, day),
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": float(day),
            "amount": float(day),
        }
        for day in range(1, 22)
    ]
    factors = calculate_raw_price_factors(pd.DataFrame(warmup + rows))
    return factors[factors["datetime"] == pd.Timestamp(2026, 7, 21)].reset_index(drop=True)


def kwargs():
    return {
        "frame": frame(),
        "partition_date": date(2026, 7, 21),
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime.fromisoformat("2026-07-20T16:00:00+08:00"),
    }


def quality_document(generation, frame):
    definition = FactorRegistry(ROOT).get("basic", "1.0.0")
    return {
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": _validate_factor_frame(frame, definition)["checks"],
        "errors": [],
        "warnings": [],
    }


def publish(tmp_path):
    arguments = kwargs()
    generation = factor_generation(**arguments)
    report_path = tmp_path / "reports" / "factor_v1" / generation / "report.json"
    if not (tmp_path / "reports").exists():
        QualityReportStore().save(tmp_path, quality_document(generation, arguments["frame"]))
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    return FactorStore(tmp_path, FactorRegistry(ROOT)).publish(**arguments)


def test_publish_nested_path_and_readback(tmp_path):
    partition = publish(tmp_path)
    expected = (
        tmp_path
        / "factors"
        / "dataset=bars_daily/schema_version=research-v1/factor_set=basic/factor_version=1.0.0/date=2026-07-21"
    )
    assert partition == expected
    result = read_factor_partition(partition)
    assert len(result) == 1
    manifest = json.loads((partition / "manifest.json").read_text())
    assert manifest["generation_id"]
    assert manifest["manifest_digest_sha256"]


def test_immutable_overwrite_rejected(tmp_path):
    publish(tmp_path)
    with pytest.raises(
        ContractError, match="immutable factor partition already published"
    ):
        publish(tmp_path)


def test_tampered_manifest_data_fail_closed(tmp_path):
    partition = publish(tmp_path)
    path = partition / "data.parquet"
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ContractError, match="tampered factor data"):
        read_factor_partition(partition)

    manifest = json.loads((partition / "manifest.json").read_text())
    manifest["logical_fingerprint"] = "f" * 64
    (partition / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match="tampered factor manifest"):
        read_factor_partition(partition)


def test_duplicate_keys_and_empty_rejected(tmp_path):
    store = FactorStore(tmp_path, FactorRegistry(ROOT))
    duplicated = pd.concat([frame(), frame()])
    with pytest.raises(ContractError, match="duplicate factor keys"):
        store.publish(frame=duplicated, **{
            key: value for key, value in kwargs().items() if key != "frame"
        })


def test_store_honors_injected_registry_at_generation_and_read(tmp_path):
    # cr.md H10 regression: factor_generation/read_factor_partition rebuilt a
    # registry from the module path, so a store whose injected registry
    # diverged from the live tree published partitions that failed their own
    # identity and quality verification.
    custom_root = tmp_path / "registry"
    definitions_dir = custom_root / "config" / "factor-sets"
    definitions_dir.mkdir(parents=True)
    document = json.loads((ROOT / "config/factor-sets/basic-v1.json").read_text())
    document["quality_thresholds"]["null_rate_maximum"] = 0.9  # live tree allows only 0.5
    (definitions_dir / "basic-v1.json").write_text(json.dumps(document))
    registry = FactorRegistry(custom_root)

    sparse = pd.DataFrame([
        {"instrument": "600000.XSHG", "datetime": pd.Timestamp(2026, 7, day),
         "range_ratio_1d": (0.5 if day == 1 else None)}
        for day in range(1, 5)
    ])  # null rate 0.75: rejected by the live registry, accepted by the injected one
    arguments = kwargs()
    arguments["frame"] = sparse
    generation = factor_generation(**{**arguments, "registry": registry})
    definition = registry.get("basic", "1.0.0")
    QualityReportStore().save(tmp_path, {
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": _validate_factor_frame(sparse, definition)["checks"],
        "errors": [],
        "warnings": [],
    })
    arguments["quality_report_checksum"] = file_sha256_bytes(
        (tmp_path / "reports" / "factor_v1" / generation / "report.json").read_bytes()
    )
    store = FactorStore(tmp_path, registry)
    store.publish(**arguments)
    _, restored = store.read_partition(generation)
    assert len(restored) == 4


def test_naive_upstream_timestamp_is_a_typed_refusal(tmp_path):
    """cr.md P4: a naive instant fails as ContractError, never a bare TypeError.

    ``FactorGovernance.validate_manifest`` compares every input's
    ``upstream_created_at`` against a tz-aware ``run_visible_cutoff``, so a naive
    value used to reach that comparison and escape as
    ``TypeError: can't compare offset-naive and offset-aware datetimes``.
    """
    store = FactorStore(tmp_path, FactorRegistry(ROOT))
    arguments = kwargs()
    for naive in (datetime(2026, 7, 20, 16, 0), "2026-07-20T16:00:00"):
        with pytest.raises(ContractError, match="must be timezone-aware"):
            store.publish(**{**arguments, "upstream_created_at": naive})
    with pytest.raises(ContractError, match="not an ISO-8601 timestamp"):
        store.publish(**{**arguments, "upstream_created_at": "20th-of-July-2026"})
