"""Guard for one remaining contract divergence (see ``pendding.md``).

The first test is a deliberate **pin, not an endorsement**: it asserts that the
divergence still exists, so fixing it forces a conscious update here instead of
letting it drift silently into new code.

1. ``uq.contracts.canonical_v2.CanonicalV2Store`` writes ``manifest_version: 2``
   manifests, while ``uq.store.reader.ManifestFirstReader`` validates against
   ``canonical-v1.json`` only, so partitions written by the former are not
   readable through the governed read path that every layer uses.

The second test used to be the matching pin for the factor quality contract --
``FactorEngine.create_result`` produced a check set that ``FactorStore.publish``
could never accept, so the governed facade could plan but never publish. That
divergence is fixed (cr.md R1), so it is now a positive lock: the engine report
has to satisfy the same validator publication re-derives, end to end.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import pytest

from tests.test_publish_factor_partitions import V2_SCHEMA, bars
from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.factors.engine import FactorEngine
from uq.factors.raw_price import calculate_raw_price_factors
from uq.factors.store import (
    FactorStore,
    _reports_agree,
    _validate_factor_frame,
    factor_generation,
)
from uq.store.reader import ManifestFirstReader

ROOT = Path(__file__).resolve().parents[1]


def test_canonical_v2_store_output_is_not_readable_by_the_governed_reader(tmp_path):
    schema = load_schema(V2_SCHEMA)
    day = date(2026, 3, 2)
    frame = bars(day)
    store = CanonicalV2Store(tmp_path)
    generation = store.prepare_generation(schema, day, frame, {}, {})
    directory = QualityReportStore().save(tmp_path, {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                    "level": "error", "result": "passed"}],
        "errors": [], "warnings": [],
    })
    store.publish(schema, day, frame, {}, {},
                  quality_checksum=file_sha256_bytes((directory / "report.json").read_bytes()))

    with pytest.raises(ContractError, match="manifest schema validation failed"):
        ManifestFirstReader(tmp_path).read(schema, day)
    # The partition really is there: the failure is the manifest contract, not
    # missing data. If this ever stops raising, converge the two writers and
    # delete this pin in the same change.
    assert (tmp_path / "canonical" / schema.dataset / schema.version
            / f"date={day.isoformat()}" / "manifest.json").is_file()


def _engine_publication_setup(tmp_path):
    registry = FactorRegistry(ROOT)
    days = pd.bdate_range("2026-06-01", periods=22)
    bars_frame = pd.DataFrame({
        "instrument": "600000.XSHG", "datetime": days, "open": 10.0, "high": 11.0,
        "low": 9.0, "close": 10.0, "volume": 100.0, "amount": 1000.0,
    })
    computed = calculate_raw_price_factors(bars_frame)
    day = date(2026, 6, 26)
    frame = computed[computed["datetime"] == pd.Timestamp(day)].reset_index(drop=True)
    engine = FactorEngine(tmp_path, registry,
                          run_visible_cutoff=pd.Timestamp("2026-08-01T00:00:00Z").to_pydatetime())
    request = engine.build_request(
        trade_date=day, factor_set="basic", factor_version="1.0.0",
        session_dates=(day,), intent="publication",
    )
    return engine, request, frame


def _publication_arguments(frame: pd.DataFrame) -> dict:
    return {
        "frame": frame,
        "partition_date": date(2026, 6, 26),
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime(2026, 6, 26, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    }


def test_factor_engine_quality_report_publishes_end_to_end(tmp_path):
    registry = FactorRegistry(ROOT)
    engine, request, frame = _engine_publication_setup(tmp_path)
    result = engine.create_result(request, frame, input_lineage=())
    local = _validate_factor_frame(frame, registry.get("basic", "1.0.0"))

    # One decision, two derivations: the report the engine hands out has to *be*
    # the report publication re-derives -- byte-for-byte, not merely agreeable --
    # otherwise the durable report checksum drifts between plan and publish.
    assert result.quality_report == local
    assert result.quality_report["status"] == local["status"] == "passed"
    assert result.errors == ()
    assert _reports_agree(local, result.quality_report) is True

    arguments = _publication_arguments(frame)
    generation = factor_generation(**arguments)
    report = {
        **result.quality_report,
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
    }
    QualityReportStore().save(tmp_path, report)
    arguments["quality_report_checksum"] = file_sha256_bytes(
        (tmp_path / "reports" / "factor_v1" / generation / "report.json").read_bytes()
    )
    partition = FactorStore(tmp_path, registry).publish(**arguments)
    assert (partition / "manifest.json").is_file()


def test_downgraded_engine_report_still_rejects_publication(tmp_path):
    """A schema-valid report that disagrees with the local decision cannot publish."""
    registry = FactorRegistry(ROOT)
    engine, request, frame = _engine_publication_setup(tmp_path)
    result = engine.create_result(request, frame, input_lineage=())
    arguments = _publication_arguments(frame)
    generation = factor_generation(**arguments)
    report = {
        **result.quality_report,
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
        "status": "warning",
        "warnings": ["downgraded after the reviewed checks passed"],
    }
    QualityReportStore().save(tmp_path, report)
    arguments["quality_report_checksum"] = file_sha256_bytes(
        (tmp_path / "reports" / "factor_v1" / generation / "report.json").read_bytes()
    )
    with pytest.raises(ContractError, match="quality report rejects publication"):
        FactorStore(tmp_path, registry).publish(**arguments)
