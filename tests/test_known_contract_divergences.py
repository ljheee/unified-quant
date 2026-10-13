"""Pins for two known contract divergences (see ``pendding.md``).

These tests are deliberate **pins, not endorsements**: each one asserts that a
divergence still exists, so fixing it forces a conscious update here instead of
letting the divergence drift silently into new code.

1. ``uq.contracts.canonical_v2.CanonicalV2Store`` writes ``manifest_version: 2``
   manifests, while ``uq.store.reader.ManifestFirstReader`` validates against
   ``canonical-v1.json`` only, so partitions written by the former are not
   readable through the governed read path that every layer uses.
2. ``FactorEngine.create_result`` emits one ``null_rate`` check per value column,
   and ``FactorStore`` rejects duplicate check names outright, so an
   engine-produced report can never satisfy publication; the published report has
   to come from ``_validate_factor_frame``.
"""

from __future__ import annotations

from datetime import date
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
from uq.factors.store import _reports_agree, _validate_factor_frame
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


def test_factor_engine_quality_report_cannot_satisfy_publication(tmp_path):
    registry = FactorRegistry(ROOT)
    days = pd.bdate_range("2026-06-01", periods=22)
    bars_frame = pd.DataFrame({
        "instrument": "600000.XSHG", "datetime": days, "open": 10.0, "high": 11.0,
        "low": 9.0, "close": 10.0, "volume": 100.0, "amount": 1000.0,
    })
    computed = calculate_raw_price_factors(bars_frame)
    engine = FactorEngine(tmp_path, registry,
                          run_visible_cutoff=pd.Timestamp("2026-08-01T00:00:00Z").to_pydatetime())
    request = engine.build_request(
        trade_date=date(2026, 6, 26), factor_set="basic", factor_version="1.0.0",
        session_dates=(date(2026, 6, 26),), intent="publication",
    )
    result = engine.create_result(request, computed, input_lineage=())
    local = _validate_factor_frame(computed, registry.get("basic", "1.0.0"))

    engine_names = [check["name"] for check in result.quality_report["checks"]]
    assert engine_names.count("null_rate") > 1, "engine checks are no longer duplicated"
    with pytest.raises(ContractError, match="duplicate quality checks reject publication"):
        _reports_agree(local, result.quality_report)
