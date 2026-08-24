import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError
from uq.factors.engine import FactorEngine
from uq.factors.store import FactorStore, factor_generation, read_factor_partition


ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
SNAPSHOT = "a" * 64
TABLE = "b" * 64


def frame(days=22):
    return pd.DataFrame({
        "instrument": ["A"] * days,
        "datetime": pd.to_datetime([date(2026, 8, 1 + day) for day in range(days)]),
        "close": [10.0 + day / 100 for day in range(days)],
        "adj_factor": [2.0] * days,
    })


def request(**kwargs):
    engine = FactorEngine(ROOT, FactorRegistry(ROOT), run_visible_cutoff=CUTOFF)
    return engine.build_request(
        trade_date=date(2026, 8, 21), factor_set="adjusted", factor_version="1.0.0", **kwargs
    )


def test_reviewed_adjusted_set_executes_through_engine():
    result = FactorEngine(ROOT, FactorRegistry(ROOT), run_visible_cutoff=CUTOFF).execute(
        request(), frame(),
        adjustment_snapshot_ids=[SNAPSHOT] * 2,
        effective_date_table_checksums=[TABLE] * 2,
    )
    assert result.status == "rejected"
    assert result.errors
    expected = {"return_1d", "return_5d", "return_20d", "volatility_20d"}
    assert expected == {item["name"] for item in result.definitions}
    assert result.input_lineage[0]["adjustment_snapshot_id"] == SNAPSHOT
    assert result.frame["return_1d"].notna().any()


def test_engine_rejects_missing_adjustment_lineage():
    with pytest.raises(ContractError, match="requires governed adjustment lineage"):
        FactorEngine(ROOT, FactorRegistry(ROOT), run_visible_cutoff=CUTOFF).execute(request(), frame())


def _publish(tmp_path: Path):
    store = FactorStore(tmp_path, FactorRegistry(ROOT))
    output = frame().iloc[-1:].copy()
    for column in ["return_1d", "return_5d", "return_20d", "volatility_20d"]:
        output[column] = 0.01
    generation_id = factor_generation(
        factor_set="adjusted", factor_version="1.0.0",
        frame=output, partition_date=date(2026, 8, 21),
        input_dataset="bars_adjusted", input_schema_version="adjusted-v1",
        upstream_generation_id="d" * 64, upstream_data_checksum="e" * 64,
        quality_report_checksum="f" * 64,
        adjustment_snapshot_id=SNAPSHOT,
        effective_date_table_checksum=TABLE,
        upstream_created_at="2026-08-21T00:00:00+00:00",
    )
    QualityReportStore().save(tmp_path, {
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation_id,
        "policy": "reject_all",
        "status": "passed",
        "checks": [
            {"name": "null_rate", "threshold": 0.5, "observed": 0.0, "level": "error", "result": "passed"},
            {"name": "coverage", "threshold": 0.0, "observed": 1.0, "level": "error", "result": "passed"},
        ],
        "errors": [],
        "warnings": [],
    })
    checksum = __import__("uq").contracts.canonical_v2.file_sha256_bytes(
        (tmp_path / "reports" / "factor_v1" / generation_id / "report.json").read_bytes()
    )
    partition = store.publish(
        factor_set="adjusted",
        factor_version="1.0.0",
        frame=output,
        partition_date=date(2026, 8, 21),
        input_dataset="bars_adjusted",
        input_schema_version="adjusted-v1",
        upstream_generation_id="d" * 64,
        upstream_data_checksum="e" * 64,
        quality_report_checksum=checksum,
        adjustment_snapshot_id=SNAPSHOT,
        effective_date_table_checksum=TABLE,
        upstream_created_at="2026-08-21T00:00:00+00:00",
    )
    return partition


def test_adjusted_partition_publishes_and_reads_with_lineage(tmp_path: Path):
    partition = _publish(tmp_path)
    restored = read_factor_partition(partition)
    assert set(["return_1d", "return_5d", "return_20d", "volatility_20d"]) <= set(restored.columns)


def test_adjusted_store_rejects_raw_input_binding(tmp_path: Path):
    with pytest.raises(ContractError, match="adjusted factor input binding"):
        FactorStore(tmp_path, FactorRegistry(ROOT)).publish(
            factor_set="adjusted", factor_version="1.0.0", frame=frame().iloc[-1:],
            partition_date=date(2026, 8, 21), input_dataset="bars_daily",
            input_schema_version="research-v1", upstream_generation_id="d" * 64,
            upstream_data_checksum="e" * 64, quality_report_checksum="f" * 64,
            adjustment_snapshot_id=SNAPSHOT, effective_date_table_checksum=TABLE,
            upstream_created_at=datetime.now(timezone.utc),
        )
