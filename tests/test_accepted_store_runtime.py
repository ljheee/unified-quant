from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.errors import ContractError
from uq.factors.store import FactorStore
from uq.contracts.factor_governance import FactorRegistry
from uq.models.accepted_store import AcceptedFactorIndexRuntime


def _frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=3)
    rows = []
    for i in range(2):
        for d in dates:
            rows.append({"instrument": f"INST{i}", "datetime": d, "volume_ratio_20d": 1.0 + i * 0.1})
    return pd.DataFrame(rows)


def _publish_factor(root: Path) -> Path:
    from uq.factors.store import factor_generation
    from uq.contracts.artifacts import QualityReportStore
    from uq.contracts.canonical_v2 import file_sha256_bytes
    frame = _frame()
    arguments = {
        "frame": frame,
        "partition_date": date(2026, 1, 5),
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime.fromisoformat("2026-01-04T15:00:00+08:00"),
    }
    generation = factor_generation(**arguments)
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    if not (root / "reports").exists():
        QualityReportStore().save(root, {
            "report_version": 1, "binding_type": "factor_v1",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": [
                {"name": "null_rate", "threshold": 0.5, "observed": 0.0, "level": "error", "result": "passed"},
                {"name": "coverage", "threshold": 0.0, "observed": 1.0, "level": "error", "result": "passed"},
            ],
            "errors": [], "warnings": [],
        })
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    registry = FactorRegistry(Path(__file__).resolve().parents[1])
    return FactorStore(root, registry).publish(**{k: v for k, v in arguments.items() if k != "frame"}, frame=frame)


class TestAcceptedFactorIndexRuntime:
    def test_list_returns_published_partitions(self, tmp_path: Path) -> None:
        _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        query = {
            "contract_version": 1,
            "filters": {},
            "ordering": ["factor_set", "partition_date"],
            "visibility": "accepted_only",
            "pagination": {"limit": 10},
        }
        results = runtime.list(query)
        assert len(results) == 1
        assert results[0]["factor_set"] == "basic"
        assert results[0]["quality_status"] == "passed"

    def test_read_verified_generation(self, tmp_path: Path) -> None:
        _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        query = {
            "contract_version": 1,
            "filters": {},
            "ordering": ["partition_date"],
            "visibility": "accepted_only",
            "pagination": {"limit": 10},
        }
        entries = runtime.list(query)
        gen_id = entries[0]["generation_id"]
        frame = runtime.read(gen_id)
        assert list(frame.columns) == ["instrument", "datetime", "volume_ratio_20d"]

    def test_unverified_generation_rejected_on_read(self, tmp_path: Path) -> None:
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        with pytest.raises(ContractError, match="not verified"):
            runtime.read("f" * 64)

    def test_filter_by_generation(self, tmp_path: Path) -> None:
        _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        base_query = {
            "contract_version": 1,
            "filters": {},
            "ordering": ["partition_date"],
            "visibility": "accepted_only",
            "pagination": {"limit": 10},
        }
        all_entries = runtime.list(base_query)
        target_gen = all_entries[0]["generation_id"]
        filtered_query = {
            **base_query,
            "filters": {"generation_id": target_gen},
        }
        results = runtime.list(filtered_query)
        assert len(results) == 1 and results[0]["generation_id"] == target_gen

    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        query = {
            "contract_version": 1,
            "filters": {}, "ordering": ["partition_date"],
            "visibility": "accepted_only", "pagination": {"limit": 10},
        }
        assert runtime.list(query) == []
