from datetime import datetime, timedelta, timezone
import json

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
