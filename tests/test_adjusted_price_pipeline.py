"""The reviewed adjusted-price line producer (cr.md: bars_adjusted/adjusted-v2).

The v1 line was named by the factor store's reviewed rule but never shipped a
schema, so no canonical adjusted partition could be published at all.  This locks
the new producer's three enforced properties:

* a partition whose restatement does not match ``price * adj_factor`` is never
  published (and never gets a "passed" report);
* an adj_factor that is missing or non-positive is refused;
* the published partition reads back through the canonical-v2 reader with the
  same identities the report binds.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, read_canonical_v2
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.pipeline.adjusted_price import (
    ADJUSTED_V2_SCHEMA,
    publish_adjusted_partition,
    restate,
    verify_positive,
    verify_restatement,
)

ROOT = Path(__file__).resolve().parents[1]
SESSION = date(2026, 8, 21)


def _bars(**overrides):
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG", "000001.XSHE"],
        "session_date": pd.to_datetime([SESSION, SESSION]),
        "open": [10.0, 20.0],
        "high": [10.5, 21.0],
        "low": [9.5, 19.0],
        "close": [10.2, 20.4],
        "volume": [1000.0, 2000.0],
        "amount": [10200.0, 40800.0],
        "status": ["trading", "trading"],
        "limit_up": [11.22, 22.44],
        "limit_down": [9.18, 18.36],
        "adj_factor": [2.0, 0.5],
    })
    for column, value in overrides.items():
        frame[column] = value
    return frame


def _independent(raw: pd.DataFrame):
    """A second, independently derived restatement (here: a different code path
    computing the same product) that the producer must be compared against."""
    out = raw.copy()
    for column in ("open", "high", "low", "close"):
        out[column] = raw[column].astype(float) * raw["adj_factor"].astype(float)
    return out


def _published(tmp_path: Path, frame=None):
    store = CanonicalV2Store(tmp_path)
    frame = _bars() if frame is None else frame
    return publish_adjusted_partition(
        store, SESSION, frame, expected_restated=_independent(frame),
        lineage={"upstream": ["bars_daily/v2"]},
        source_versions={"tushare": "test"},
    ), store


def test_restatement_multiplies_every_price_column():
    restated = restate(_bars())
    for column in ("open", "high", "low", "close"):
        expected = _bars()[column] * _bars()["adj_factor"]
        pd.testing.assert_series_equal(
            restated[column].astype(float), expected.astype(float), check_names=False
        )
    # identity columns are untouched
    pd.testing.assert_frame_equal(restated[["instrument", "session_date"]], _bars()[["instrument", "session_date"]])


def test_missing_adj_factor_is_refused():
    frame = _bars(adj_factor=[None, 0.5])
    with pytest.raises(ContractError, match="adj_factor is missing"):
        restate(frame)


def test_non_positive_adj_factor_is_refused():
    frame = _bars(adj_factor=[0.0, 0.5])
    with pytest.raises(ContractError, match="adj_factor is non-positive"):
        restate(frame)


def test_wrong_restatement_is_refused_before_publication(tmp_path):
    # The frame is schema-legal (invariants hold on its own numbers) but the
    # producer must still refuse it because the prices it carries are not the
    # restatement of the vendor prices by adj_factor.  Nothing is published and
    # no quality report claims "passed".
    raw = _bars()
    drifted = raw.copy()
    # a 1% price drift, far outside the 1e-3 tolerance, while every invariant
    # (high >= close, low <= close, adj_factor > 0) still holds
    drifted["close"] = raw["close"] * 1.01
    drifted["high"] = raw["high"] * 1.01
    drifted["low"] = raw["low"] * 1.01
    drifted["open"] = raw["open"] * 1.01
    drifted["amount"] = raw["amount"] * 1.01
    store = CanonicalV2Store(tmp_path)
    # Either of the two checks may fire first (the producer verifies its own
    # restatement, then the independent one); what matters is that the partition
    # is refused before publication and no report claims "passed".
    with pytest.raises(ContractError) as refusal:
        publish_adjusted_partition(
            store, SESSION, drifted, expected_restated=_independent(raw), source_versions={"tushare": "test"}
        )
    assert "does not equal" in str(refusal.value) or "independent restatement" in str(refusal.value)
    partition = tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={SESSION.isoformat()}"
    assert not partition.exists()
    assert not (tmp_path / "reports").exists()


def test_non_positive_restated_price_is_refused():
    # Driven on an already-restated frame: `restate` itself refuses a non-positive
    # factor, so this isolates the sign rule the review asked for (a restated
    # price of zero would silently divide-by-zero into the factor layer).
    restated = _bars().copy()
    restated["close"] = [0.0, 20.4]
    with pytest.raises(ContractError, match="non-positive"):
        verify_positive(restated)


def test_published_partition_reads_back_with_the_bound_generation(tmp_path):
    payload, store = _published(tmp_path)
    manifest = json.loads(
        (tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={SESSION.isoformat()}" / "manifest.json").read_text()
    )
    assert manifest["generation_id"] == payload["generation_id"]
    # the report the store read back is the one bound to that generation
    report = QualityReportStore().read(
        tmp_path, payload["generation_id"], binding_type="canonical_v2"
    )
    assert report["policy"] == "reject_all"
    assert report["status"] == "passed"
    names = {check["name"] for check in report["checks"]}
    assert {
        "coverage",
        "semantic_version",
        "finite_values__restated_price_recomputes",
        "finite_values__restated_price_matches_independent_source",
        "finite_values__restated_price_positive",
    } <= names
    # and the data itself is the restated frame
    restored = read_canonical_v2(
        tmp_path, load_schema(ADJUSTED_V2_SCHEMA), SESSION,
        expected_anchor=manifest["trust_anchor_sha256"],
    )
    assert sorted(restored["close"]) == sorted((_bars()["close"] * _bars()["adj_factor"]).tolist())


def test_schema_marks_the_price_columns_as_back_adjusted():
    schema = load_schema(ADJUSTED_V2_SCHEMA)
    assert schema.version == "adjusted-v2"
    for column in ("open", "high", "low", "close"):
        assert schema.fields[column]["adjustment"] == "back_adjusted"
    # units and non-price fields are inherited from the reviewed v2 input
    bars = load_schema("config/schemas/bars_daily.v2.yaml")
    assert schema.fields["volume"]["unit"] == bars.fields["volume"]["unit"]
    assert schema.fields["adj_factor"]["semantics"] == bars.fields["adj_factor"]["semantics"]
    assert schema.invariants == bars.invariants


def test_republishing_the_same_partition_is_immutable(tmp_path):
    _published(tmp_path)
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="already published"):
        publish_adjusted_partition(
            store, SESSION, _bars(), expected_restated=_independent(_bars()), source_versions={"tushare": "test"}
        )


def test_publication_without_an_independent_restatement_is_refused(tmp_path):
    # Restating and then verifying one's own output is a tautology that would
    # stamp reject_all/passed on anything, so the cross-check is mandatory.
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="requires an independently restated partition"):
        publish_adjusted_partition(
            store, SESSION, _bars(), expected_restated=None, source_versions={"tushare": "test"}
        )


def test_a_disagreeing_independent_restatement_blocks_publication(tmp_path):
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="disagrees with the independent restatement"):
        publish_adjusted_partition(
            store, SESSION, _bars(), expected_restated=_independent(_bars()).assign(close=1.0),
            source_versions={"tushare": "test"},
        )
    assert not (tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={SESSION.isoformat()}").exists()
