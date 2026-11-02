from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from uq.adapters.tushare import TUSHARE_RESEARCH_ENDPOINTS, TushareAdapter
from uq.contracts.tushare_dependency import TushareDependencyStore
from uq.errors import ContractError


class _Client:
    def __init__(self):
        self.calls = []

    def daily_basic(self, **params):
        self.calls.append(("daily_basic", params))
        return pd.DataFrame({
            "ts_code": ["600000.SH"],
            "trade_date": ["20261030"],
            "total_mv": [100000.0],
        })

    def stock_basic(self, **params):
        self.calls.append(("stock_basic", params))
        return pd.DataFrame({
            "ts_code": ["000861.SZ"],
            "delist_date": ["20240918"],
        })


def _adapter(client):
    adapter = TushareAdapter(token_env="MISSING_UQ_TOKEN")
    adapter._client = lambda: client
    return adapter


def test_research_adapter_fetches_bounded_allowlisted_endpoint():
    client = _Client()
    result = _adapter(client).research_data("daily_basic", trade_date="20261030")

    assert result.loc[0, "total_mv"] == 100000.0
    assert client.calls == [("daily_basic", {"trade_date": "20261030"})]
    assert "report_rc" in TUSHARE_RESEARCH_ENDPOINTS


def test_research_adapter_fetches_bounded_stock_basic_delisting_dates():
    client = _Client()
    result = _adapter(client).research_data(
        "stock_basic", list_status="D", fields="ts_code,delist_date"
    )

    assert result.loc[0, "delist_date"] == "20240918"
    assert client.calls == [(
        "stock_basic", {"list_status": "D", "fields": "ts_code,delist_date"}
    )]


def test_research_adapter_fetches_daily_suspension_snapshots():
    class SuspensionClient(_Client):
        def suspend_d(self, **params):
            self.calls.append(("suspend_d", params))
            return pd.DataFrame({
                "ts_code": ["000861.SZ"],
                "trade_date": ["20261030"],
                "suspend_type": ["S"],
            })

    client = SuspensionClient()
    result = _adapter(client).research_data(
        "suspend_d", trade_date="20261030", suspend_type="S"
    )

    assert result.loc[0, "ts_code"] == "000861.SZ"
    assert client.calls == [(
        "suspend_d", {"trade_date": "20261030", "suspend_type": "S"}
    )]


@pytest.mark.parametrize(
    "endpoint,params",
    [
        ("daily_basic", {}),
        ("daily_basic", {"trade_date": ""}),
        ("top_list", {"ts_code": "600000.SH"}),
        ("index_member_all", {"is_new": "N"}),
        ("report_rc", {"start_date": "20260101"}),
        ("unsupported", {"trade_date": "20261030"}),
    ],
)
def test_research_adapter_rejects_unbounded_or_unsupported_queries(endpoint, params):
    with pytest.raises(ContractError):
        _adapter(_Client()).research_data(endpoint, **params)


def _frame():
    return pd.DataFrame({
        "ts_code": ["600000.SH", "000001.SZ"],
        "trade_date": ["20261030", "20261030"],
        "total_mv": [100000.0, 200000.0],
    })


def test_stock_basic_delisting_snapshot_is_supported_and_read_back(tmp_path):
    store = TushareDependencyStore(tmp_path)
    frame = pd.DataFrame({
        "ts_code": ["000861.SZ"],
        "delist_date": ["20240918"],
    })
    manifest = store.publish(
        "stock_basic",
        query={"list_status": "D", "fields": "ts_code,delist_date"},
        source_fetched_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        frame=frame,
    )

    loaded_manifest, loaded = store.read_frame(manifest["generation_id"], dataset="stock_basic")
    assert loaded_manifest["query"]["list_status"] == "D"
    pd.testing.assert_frame_equal(loaded, frame)


def test_suspension_snapshot_is_supported_and_read_back(tmp_path):
    store = TushareDependencyStore(tmp_path)
    frame = pd.DataFrame({
        "ts_code": ["000861.SZ"],
        "trade_date": ["20261030"],
        "suspend_type": ["S"],
    })
    manifest = store.publish(
        "suspend_d",
        query={"trade_date": "20261030", "suspend_type": "S"},
        source_fetched_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        frame=frame,
    )

    loaded_manifest, loaded = store.read_frame(manifest["generation_id"], dataset="suspend_d")
    assert loaded_manifest["query"]["suspend_type"] == "S"
    pd.testing.assert_frame_equal(loaded, frame)


def test_dependency_snapshot_is_checksum_verified_and_idempotent(tmp_path):
    store = TushareDependencyStore(tmp_path)
    query = {"trade_date": "20261030"}
    fetched_at = datetime(2026, 10, 31, 1, tzinfo=timezone.utc)
    manifest = store.publish(
        "daily_basic", query=query, source_fetched_at=fetched_at, frame=_frame()
    )
    duplicate = store.publish(
        "daily_basic",
        query=query,
        source_fetched_at=fetched_at + timedelta(hours=1),
        frame=_frame(),
    )

    assert duplicate["generation_id"] == manifest["generation_id"]
    loaded_manifest, loaded = store.read_frame(manifest["generation_id"], dataset="daily_basic")
    pd.testing.assert_frame_equal(loaded, _frame())
    assert loaded_manifest["row_count"] == 2
    assert store.existing_generation_for_query("daily_basic", query)["generation_id"] == manifest[
        "generation_id"
    ]


def test_dependency_snapshot_preserves_revisions_as_separate_generations(tmp_path):
    store = TushareDependencyStore(tmp_path)
    query = {"trade_date": "20261030"}
    first = store.publish(
        "daily_basic",
        query=query,
        source_fetched_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
        frame=_frame(),
    )
    revised_frame = _frame()
    revised_frame.loc[0, "total_mv"] = 101000.0
    second = store.publish(
        "daily_basic",
        query=query,
        source_fetched_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        frame=revised_frame,
    )

    assert first["generation_id"] != second["generation_id"]
    assert store.existing_generation_for_query("daily_basic", query)["generation_id"] == second[
        "generation_id"
    ]


def test_same_instance_indexes_a_new_query_after_query_index_was_loaded(tmp_path):
    store = TushareDependencyStore(tmp_path)
    first_query = {"trade_date": "20261029"}
    second_query = {"trade_date": "20261030"}
    assert store.existing_generation_for_query("daily_basic", first_query) is None

    published = store.publish(
        "daily_basic",
        query=second_query,
        source_fetched_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
        frame=_frame(),
    )

    found = store.existing_generation_for_query("daily_basic", second_query)
    assert found is not None
    assert found["generation_id"] == published["generation_id"]


def test_staged_dependency_payload_is_read_back_before_rename(tmp_path, monkeypatch):
    store = TushareDependencyStore(tmp_path)
    original_write_bytes = Path.write_bytes

    def corrupt_staged_payload(path: Path, payload: bytes) -> int:
        if path.parent.name.startswith(".staging_") and path.name == "data.parquet":
            payload = b"corrupt staged payload"
        return original_write_bytes(path, payload)

    monkeypatch.setattr(Path, "write_bytes", corrupt_staged_payload)
    with pytest.raises(ContractError, match="staged Tushare dependency artifact checksum mismatch"):
        store.publish(
            "daily_basic",
            query={"trade_date": "20261030"},
            source_fetched_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
            frame=_frame(),
        )
    family = tmp_path / "tushare_dependencies" / "daily_basic"
    assert not list(family.glob("generation=*"))
    assert not list(family.glob(".staging_*"))


def test_dependency_snapshot_rejects_missing_required_fields_and_credentials(tmp_path):
    store = TushareDependencyStore(tmp_path)
    with pytest.raises(ContractError, match="missing columns"):
        store.publish(
            "daily_basic",
            query={"trade_date": "20261030"},
            source_fetched_at=datetime.now(timezone.utc),
            frame=pd.DataFrame({"ts_code": ["600000.SH"]}),
        )
    with pytest.raises(ContractError, match="credentials"):
        store.publish(
            "daily_basic",
            query={"token": "must-never-be-stored"},
            source_fetched_at=datetime.now(timezone.utc),
            frame=_frame(),
        )


def test_dependency_snapshot_rejects_truncated_provider_page(tmp_path):
    store = TushareDependencyStore(tmp_path)
    truncated = pd.DataFrame({
        "ts_code": [f"600{i:03d}.SH" for i in range(6000)],
        "trade_date": ["20261030"] * 6000,
    })
    with pytest.raises(ContractError, match="reached its 6000-row limit"):
        store.publish(
            "daily_basic",
            query={"trade_date": "20261030"},
            source_fetched_at=datetime.now(timezone.utc),
            frame=truncated,
        )


def test_stock_basic_snapshot_rejects_truncated_provider_page(tmp_path):
    store = TushareDependencyStore(tmp_path)
    truncated = pd.DataFrame({
        "ts_code": [f"600{i:03d}.SH" for i in range(6000)],
        "delist_date": ["20261030"] * 6000,
    })
    with pytest.raises(ContractError, match="stock_basic response reached its 6000-row limit"):
        store.publish(
            "stock_basic",
            query={"list_status": "D", "fields": "ts_code,delist_date"},
            source_fetched_at=datetime.now(timezone.utc),
            frame=truncated,
        )


def test_suspension_snapshot_rejects_truncated_provider_page(tmp_path):
    store = TushareDependencyStore(tmp_path)
    truncated = pd.DataFrame({
        "ts_code": ["000861.SZ"] * 5000,
        "trade_date": ["20261030"] * 5000,
    })
    with pytest.raises(ContractError, match="suspend_d response reached its 5000-row limit"):
        store.publish(
            "suspend_d",
            query={"trade_date": "20261030", "suspend_type": "S"},
            source_fetched_at=datetime.now(timezone.utc),
            frame=truncated,
        )


def test_tampered_dependency_artifact_is_rejected(tmp_path):
    store = TushareDependencyStore(tmp_path)
    manifest = store.publish(
        "daily_basic",
        query={"trade_date": "20261030"},
        source_fetched_at=datetime.now(timezone.utc),
        frame=_frame(),
    )
    artifact = (
        tmp_path / "tushare_dependencies" / "daily_basic"
        / f"generation={manifest['generation_id']}" / "data.parquet"
    )
    artifact.write_bytes(b"tampered")
    with pytest.raises(ContractError, match="checksum mismatch"):
        store.read_frame(manifest["generation_id"], dataset="daily_basic")


def test_manifest_does_not_record_token_material(tmp_path):
    store = TushareDependencyStore(tmp_path)
    manifest = store.publish(
        "daily_basic",
        query={"trade_date": "20261030"},
        source_fetched_at=datetime.now(timezone.utc),
        frame=_frame(),
    )
    encoded = json.dumps(manifest, sort_keys=True)
    assert "token" not in encoded.lower()
    assert "password" not in encoded.lower()
