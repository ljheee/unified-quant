"""Regression tests for scripts/build_label_sets.py.

The label set was the last pre-model family with no production publisher. These
lock what the entry point must guarantee: the binding it stamps satisfies the
closed label_set.v1 contract, the frame it hands the builder is the one the
builder's own checksum describes (so the checksum the builder verifies is the
checksum the caller computed), and a window that cannot label is refused.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from uq.contracts.model_layer import sha256_json
from uq.contracts.schema import load_schema
from uq.research_chain.owning_contracts import LabelStore

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_label_sets", _ROOT / "scripts" / "build_label_sets.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

DATES = pd.bdate_range("2026-01-05", periods=12)
INSTRUMENTS = ("600000.XSHG", "000001.XSHE")


def _publish_bars(root: Path, instruments=INSTRUMENTS, listing_dates=None):
    schema = load_schema("config/schemas/bars_daily.v2.yaml")
    rows = []
    for index, instrument in enumerate(instruments):
        for position, day in enumerate(DATES):
            close = 10.0 + position * 0.1 + index
            rows.append({
                "instrument": instrument, "session_date": day,
                "open": close - 0.1, "high": close + 0.2, "low": close - 0.2,
                "close": close, "volume": 10000.0, "amount": 100000.0,
                "status": "trading", "limit_up": close * 1.1, "limit_down": close * 0.9,
                "adj_factor": 1.0,
            })
    frame = pd.DataFrame(rows)
    store = CanonicalV2Store(root)
    for day in DATES:
        part = frame[frame["session_date"] == day]
        generation = store.prepare_generation(schema, day.date(), part, {}, {})
        report = {
            "report_version": 1, "binding_type": "canonical_v2",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                        "level": "error", "result": "passed"}],
            "errors": [], "warnings": [],
        }
        directory = QualityReportStore().save(root, report)
        store.publish(schema, day.date(), part, {}, {},
                      quality_checksum=file_sha256_bytes((directory / "report.json").read_bytes()))
    return schema


def _patch_listing_dates(monkeypatch, listing_dates=None):
    defaults = {instrument: date(2010, 1, 4) for instrument in INSTRUMENTS}
    if listing_dates is not None:
        defaults.update(listing_dates)
    monkeypatch.setattr(module, "_listing_dates", lambda instruments: defaults)


def _run(tmp_path, monkeypatch, *, start="2026-01-05", end="2026-01-20", instrument=None,
         listing_dates=None, combined=False):
    bars_root = tmp_path / "bars"
    _publish_bars(bars_root)
    _patch_listing_dates(monkeypatch, listing_dates)
    out = tmp_path / "report.json"
    argv = [
        "--bars-root", str(bars_root), "--label-root", str(tmp_path / "labels"),
        "--start", start, "--end", end, "--out", str(out),
    ]
    if instrument is not None:
        argv += ["--instrument", instrument]
    if combined:
        argv += ["--combined"]
    code = module.main(argv)
    report = json.loads(out.read_text()) if out.is_file() else None
    return code, report


def test_publishes_one_label_set_per_instrument(tmp_path, monkeypatch):
    code, report = _run(tmp_path, monkeypatch)
    assert code == 0
    assert report["published"] == len(INSTRUMENTS)
    assert report["refused"] == 0
    # the horizon tail is null by contract, and that is reported rather than hidden
    for record in report["records"]:
        assert record["rows"] == len(DATES)
        assert record["null_labels"] == 5


def test_publishes_one_combined_label_set_for_the_whole_universe(tmp_path, monkeypatch):
    # A research request binds exactly one label set covering its whole universe
    # (`research_run_request_*.json` has a single `label_binding`, closed to
    # {family, generation_id, manifest_digest_sha256}). Per-instrument sets could
    # never be bound, so the entry point must be able to publish one combined set.
    code, report = _run(tmp_path, monkeypatch, combined=True)
    assert code == 0
    assert report["mode"] == "combined"
    assert report["published"] == 1
    assert report["refused"] == 0
    record, = report["records"]
    assert record["rows"] == len(DATES) * len(INSTRUMENTS)
    # the horizon tail is still null by contract, once per instrument
    assert record["null_labels"] == 5 * len(INSTRUMENTS)


def test_the_combined_set_reads_back_and_covers_every_instrument(tmp_path, monkeypatch):
    # The combined set is the shape the dataset stage merges against the factor
    # frame: one manifest, one identity, every instrument on every decision date.
    # A missing (instrument, decision_date) key would silently drop a training row
    # through `_merge_labels`' left join, so the grid must be complete here.
    _run(tmp_path, monkeypatch, combined=True)
    report = json.loads((tmp_path / "report.json").read_text())
    store = LabelStore(tmp_path / "labels")
    manifest, frame = store.read_frame(report["records"][0]["label_generation_id"])
    assert manifest["generation_id"] == report["records"][0]["label_generation_id"]
    assert len(frame) == len(DATES) * len(INSTRUMENTS)
    assert frame["instrument"].nunique() == len(INSTRUMENTS)
    assert not frame.duplicated(["instrument", "decision_date"]).any()
    # one adjusted-price binding, describing the whole window's bars frame
    (binding,) = manifest["upstream_adjusted_price_bindings"]
    assert len(binding["data_checksum_sha256"]) == 64


def test_combined_mode_refuses_before_any_frame_is_built(tmp_path, monkeypatch):
    # Combined mode collapses every instrument into one group, and `pd.concat`
    # raises on an empty sequence rather than recording a refusal. The selection
    # gates must therefore fire *before* the groups are built, so a combined run
    # over an empty selection is a configuration failure, not a raw ValueError.
    bars_root = tmp_path / "bars"
    _publish_bars(bars_root, instruments=INSTRUMENTS)
    _patch_listing_dates(monkeypatch)
    with pytest.raises(SystemExit, match="not present in the window's bars"):
        module.main([
            "--bars-root", str(bars_root), "--label-root", str(tmp_path / "labels"),
            "--start", "2026-01-05", "--end", "2026-01-20",
            "--instrument", "999999.XSHG", "--combined",
            "--out", str(tmp_path / "report.json"),
        ])


def test_combined_mode_publishes_nothing_when_an_eligibility_column_is_missing(tmp_path, monkeypatch):
    # The store is immutable, so a refuse-after-write would be unrecoverable. A
    # per-instrument absence must abort the whole combined run before the writer
    # opens its first directory -- `pd.concat` over a partially eligible frame is
    # exactly the case where one instrument's KeyError must not be swallowed.
    bars_root = tmp_path / "bars"
    _publish_bars(bars_root, instruments=INSTRUMENTS)
    listing = {instrument: date(2010, 1, 4) for instrument in INSTRUMENTS}
    monkeypatch.setattr(module, "_listing_dates", lambda instruments: listing)
    listing.pop("000001.XSHE")
    monkeypatch.setattr(module, "_listing_dates", lambda instruments: listing)
    with pytest.raises(KeyError):
        module.main([
            "--bars-root", str(bars_root), "--label-root", str(tmp_path / "labels"),
            "--start", "2026-01-05", "--end", "2026-01-20", "--combined",
            "--out", str(tmp_path / "report.json"),
        ])
    assert not (tmp_path / "labels").exists()


def test_published_label_sets_read_back_through_the_governed_store(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    store = LabelStore(tmp_path / "labels")
    report = json.loads((tmp_path / "report.json").read_text())
    for record in report["records"]:
        manifest, frame = store.read_frame(record["label_generation_id"])
        assert manifest["generation_id"] == record["label_generation_id"]
        assert len(frame) == record["rows"]
        assert list(frame.columns) == ["instrument", "decision_date", "label"]


def test_the_stamped_binding_satisfies_the_closed_contract(tmp_path, monkeypatch):
    # label_set.v1 closes the binding object: no `instrument` field and
    # `visible_cutoff` is required. Getting this wrong fails at publish time with a
    # schema message that does not name the caller.
    _run(tmp_path, monkeypatch)
    report = json.loads((tmp_path / "report.json").read_text())
    store = LabelStore(tmp_path / "labels")
    manifest, _ = store.read_frame(report["records"][0]["label_generation_id"])
    (binding,) = manifest["upstream_adjusted_price_bindings"]
    assert set(binding) == {
        "binding", "dataset", "schema_version", "partition_date",
        "generation_id", "data_checksum_sha256", "visible_cutoff",
    }
    assert binding["binding"] == "adjusted_price"


def test_the_binding_checksum_is_the_one_the_builder_verifies(tmp_path, monkeypatch):
    # The builder refuses a publish unless the binding's data_checksum_sha256 equals
    # the checksum it computes over the frame it was handed, so the caller must use
    # the same function (sha256_json over canonical JSON), not plain json.dumps.
    code, report = _run(tmp_path, monkeypatch)
    assert code == 0
    assert report["refused"] == 0


def test_an_instrument_without_a_listing_date_is_a_configuration_failure(tmp_path, monkeypatch):
    # The real `_listing_dates` is exercised (not patched out), with a security
    # master that does not cover the instrument: guessing a listing date would be a
    # silent point-in-time assumption about label eligibility.
    bars_root = tmp_path / "bars"
    _publish_bars(bars_root, instruments=("600000.XSHG",))
    live = pd.DataFrame({"ts_code": ["600000.SH"], "list_date": ["20100104"]})
    delisted = pd.DataFrame({"ts_code": [], "list_date": []})
    # Both statuses are fetched (a delisted name still needs its listing date), and
    # neither of them covers the instrument under test.
    monkeypatch.setattr(
        module.Tushare2kAdapter, "stock_basic",
        lambda self, *, list_status="L": live if list_status == "L" else delisted,
    )
    with pytest.raises(SystemExit, match="no listing date"):
        module._listing_dates(["600000.XSHG", "000001.XSHE"])


def test_a_delisted_instrument_still_gets_its_listing_date(monkeypatch):
    # A delisted name dropped by the default `L` filter would make a whole window
    # unlabellable for no reason: it still traded through the window, so its
    # decision dates are labelable. Measured case: 601989 (中国重工), absent under
    # L, present under D with list_date=20091216.
    live = pd.DataFrame({"ts_code": ["600000.SH"], "list_date": ["20100104"]})
    delisted = pd.DataFrame({"ts_code": ["601989.SH"], "list_date": ["20091216"]})
    monkeypatch.setattr(
        module.Tushare2kAdapter, "stock_basic",
        lambda self, *, list_status="L": live if list_status == "L" else delisted,
    )
    resolved = module._listing_dates(["600000.XSHG", "601989.XSHG"])
    assert resolved == {
        "600000.XSHG": date(2010, 1, 4),
        "601989.XSHG": date(2009, 12, 16),
    }


def test_a_live_row_wins_over_a_delisted_row_for_the_same_code(monkeypatch):
    # A code can appear in both sets if the provider is laggard; the listing date is
    # identical either way, so keep the live one deterministically.
    live = pd.DataFrame({"ts_code": ["600000.SH"], "list_date": ["19991110"]})
    delisted = pd.DataFrame({"ts_code": ["600000.SH"], "list_date": ["19991110"]})
    monkeypatch.setattr(
        module.Tushare2kAdapter, "stock_basic",
        lambda self, *, list_status="L": live if list_status == "L" else delisted,
    )
    assert module._listing_dates(["600000.XSHG"]) == {"600000.XSHG": date(1999, 11, 10)}


def test_an_unknown_instrument_filter_is_a_configuration_failure(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="not present in the window's bars"):
        _run(tmp_path, monkeypatch, instrument="999999.XSHG")


def test_a_window_with_no_bars_is_refused(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="no bars partitions"):
        _run(tmp_path, monkeypatch, start="2030-01-05", end="2030-01-20")
