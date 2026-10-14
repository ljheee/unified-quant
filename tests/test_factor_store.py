import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError
from uq.factors.qlib_adapter import QlibNotInstalledError
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


def test_unreviewed_input_binding_names_the_reviewed_line(tmp_path, monkeypatch):
    """cr.md P3: a refusal must name the reviewed line so the caller can act.

    These two input-binding guards are why governed factors cannot consume the
    production ``bars_daily/v2`` line.  A bare "not reviewed" message cannot be
    told apart from a wrong argument, so both rejections now quote the reviewed
    dataset/schema pair.
    """
    store = FactorStore(tmp_path, FactorRegistry(ROOT))
    with pytest.raises(ContractError) as basic:
        store.publish(**{**kwargs(), "input_schema_version": "v2"})
    assert "basic/1.0.0: got bars_daily/v2" in str(basic.value)
    assert "reviewed binding is bars_daily/research-v1" in str(basic.value)

    # The Qlib branch is only reachable through a definition whose output columns
    # the fixture frame satisfies, so derive one from the reviewed alpha158
    # definition and swap in the basic set's factor names.  The point under test is
    # the input-schema guard, which runs before any Qlib computation: the same
    # fixture with input_schema_version="research-v1" gets past it and only then
    # fails on the missing optional qlib extra.
    basic_document = json.loads((ROOT / "config/factor-sets/basic-v1.json").read_text())
    alpha_document = json.loads((ROOT / "config/factor-sets/alpha158-v1.json").read_text())
    factors = []
    for alpha_factor, basic_factor in zip(alpha_document["factors"], basic_document["factors"]):
        entry = dict(alpha_factor)
        entry.update({
            "name": basic_factor["name"],
            "version": basic_factor["version"],
            "implementation_fingerprint": basic_factor["implementation_fingerprint"],
        })
        factors.append(entry)
    alpha_document.update({"factors": factors, "required_columns": basic_document["required_columns"], "excluded_factors": []})
    project_root = tmp_path / "project"
    definitions = project_root / "config" / "factor-sets"
    definitions.mkdir(parents=True)
    (definitions / "alpha158-v1.json").write_text(json.dumps(alpha_document), encoding="utf-8")
    alpha_store = FactorStore(tmp_path / "alpha", FactorRegistry(project_root))
    with pytest.raises(ContractError) as alpha:
        alpha_store.publish(**{
            **kwargs(), "factor_set": "alpha158", "factor_version": "1.0.0",
            "input_schema_version": "v2",
        })
    assert "factor input schema is not reviewed: v2" in str(alpha.value)
    assert "reviewed schema versions for bars_daily: ['research-v1']" in str(alpha.value)

    # The narrated continuation is executed too (cr.md feat MINOR-3): past the
    # schema guard, the same fixture reaches the Qlib dependency and fails on
    # the missing optional extra -- proving the guard runs before any Qlib
    # computation rather than merely claiming so.  The absence is pinned, not
    # sampled from the interpreter (same idiom as test_qlib_adapter): four
    # certified CI cells install pyqlib through the qlib extra, where the real
    # import succeeds and this assertion could not hold.
    monkeypatch.setitem(sys.modules, "qlib", None)
    with pytest.raises(QlibNotInstalledError, match="pyqlib"):
        alpha_store.publish(**{
            **kwargs(), "factor_set": "alpha158", "factor_version": "1.0.0",
        })
