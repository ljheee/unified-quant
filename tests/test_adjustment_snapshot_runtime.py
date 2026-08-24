import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.adjustment_snapshot import AdjustmentSnapshotReader, AdjustmentSnapshotStore
from uq.market.adjustment import XdxrAdjustmentDeriver
from uq.errors import ContractError


def event(day=21, cash=0.0, bonus=0.0, rights=0.0, rights_price=0.0):
    return {"year": 2026, "month": 8, "day": day, "category": 1,
            "fenhong": cash, "songzhuangu": bonus, "peigu": rights, "peigujia": rights_price}


def test_exchange_formula_golden_cases():
    fixture = json.loads(Path("config/schemas/fixtures/adjustment/formula-golden.json").read_text())
    deriver = XdxrAdjustmentDeriver()
    for case in fixture["cases"]:
        events = pd.DataFrame([event(cash=case["cash_per_ten"], bonus=case["bonus_per_ten"], rights=case["rights_per_ten"], rights_price=case["rights_price"])])
        pre_close = pd.Series([case["pre_close"]], index=pd.to_datetime(["2026-08-21"]))
        derivation = deriver.derive("600000.XSHG", events, pd.to_datetime(["2026-08-20", "2026-08-21"]), pre_close)
        multiplier = float(derivation.frame.iloc[0]["adj_factor"])
        assert multiplier == pytest.approx(case["expected_backward_multiplier"], abs=case["tolerance_abs"])
        ex_right = case["pre_close"] / multiplier
        assert ex_right == pytest.approx(case["expected_ex_right_price"], abs=case["tolerance_abs"])


def test_multiple_events_and_non_session_event_are_ordered():
    sessions = pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-22"])
    events = pd.DataFrame([
        event(day=20, cash=1.0),
        event(day=21, cash=2.0, bonus=1.0),
        event(day=23, rights=1.0, rights_price=9.0),
    ])
    pre_close = pd.Series([10.0, 11.0, 12.0], index=sessions)
    derivation = XdxrAdjustmentDeriver().derive("000001.XSHE", events, sessions, pre_close)
    assert len(derivation.frame) == 3
    assert derivation.frame.iloc[-1]["adj_factor"] == pytest.approx(1.0)
    assert derivation.frame.iloc[0]["adj_factor"] != pytest.approx(1.0)


def _snapshot(root, visibility="2026-08-21T16:00:00+08:00"):
    return AdjustmentSnapshotStore().save(
        root,
        instrument="600000.XSHG",
        visibility_time=datetime.fromisoformat(visibility),
        window_start=date(2026, 8, 20), window_end=date(2026, 8, 21),
        events=pd.DataFrame([event()]),
        effective_dates=pd.DataFrame({"session_date":["2026-08-20","2026-08-21"],"effective_date":[None,"2026-08-20"],"source_event_id":["","event"]}),
        sessions=[date(2026,8,20), date(2026,8,21)],
    )


def test_snapshot_persistence_checksums_visibility_and_typed_reader(tmp_path):
    directory = _snapshot(tmp_path)
    generation_id = directory.name
    reader = AdjustmentSnapshotReader(tmp_path)
    manifest = reader.read_manifest("600000.XSHG", generation_id)
    assert manifest["formula_version"] == "adj_factor.exchange_v1"
    assert list(reader.read_events("600000.XSHG", generation_id).columns)[0] == "year"
    assert reader.read_effective_dates("600000.XSHG", generation_id).shape[0] == 2
    selected = reader.select_visible_generation(
        instrument="600000.XSHG", window_start=date(2026,8,20), window_end=date(2026,8,21),
        as_of_time=datetime.fromisoformat("2026-08-21T08:00:00+00:00"), candidates=[generation_id],
    )
    assert selected == generation_id
    with pytest.raises(ContractError, match="ambiguous or absent visible adjustment snapshot"):
        reader.select_visible_generation(
            instrument="600000.XSHG", window_start=date(2026,8,20), window_end=date(2026,8,21),
            as_of_time=datetime.fromisoformat("2026-08-21T07:59:00+00:00"), candidates=[generation_id],
        )


def test_snapshot_rejects_session_manifest_mismatch(tmp_path):
    directory = _snapshot(tmp_path)
    generation_id = directory.name
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sessions"]["end_date"] = "2026-08-22"
    manifest["generation_id"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest))
    reader = AdjustmentSnapshotReader(tmp_path)
    with pytest.raises(ContractError, match="generation mismatch"):
        reader.select_visible_generation(
            instrument="600000.XSHG", window_start=date(2026,8,20), window_end=date(2026,8,21),
            as_of_time=datetime.fromisoformat("2026-08-21T08:00:00+00:00"), candidates=[generation_id],
        )


def test_snapshot_rejects_missing_sessions_artifact(tmp_path):
    directory = _snapshot(tmp_path)
    (directory / "sessions.csv").unlink()
    reader = AdjustmentSnapshotReader(tmp_path)
    with pytest.raises(ContractError, match="missing adjustment snapshot artifact"):
        reader.select_visible_generation(
            instrument="600000.XSHG", window_start=date(2026,8,20), window_end=date(2026,8,21),
            as_of_time=datetime.fromisoformat("2026-08-21T08:00:00+00:00"), candidates=[directory.name],
        )


def test_snapshot_immutability_and_tamper_fail_closed(tmp_path):
    directory = _snapshot(tmp_path)
    with pytest.raises(ContractError, match="immutable adjustment snapshot already published"):
        _snapshot(tmp_path)
    (directory / "events.csv").write_text((directory / "events.csv").read_text().replace("category", "kind"))
    reader = AdjustmentSnapshotReader(tmp_path)
    with pytest.raises(ContractError):
        reader.read_events("600000.XSHG", directory.name)


def test_snapshot_rejects_unsafe_artifact_path_and_extra_files(tmp_path):
    directory = _snapshot(tmp_path)
    generation_id = directory.name
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["event_artifact"]["path"] = "../events.csv"
    manifest["generation_id"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    reader = AdjustmentSnapshotReader(tmp_path)
    with pytest.raises(ContractError, match="is not one of"):
        reader.load("600000.XSHG", generation_id)

    directory = _snapshot(tmp_path / "clean")
    (directory / "extra.txt").write_text("unexpected")
    with pytest.raises(ContractError, match="unexpected adjustment snapshot files"):
        AdjustmentSnapshotReader(tmp_path / "clean").load("600000.XSHG", directory.name)



def test_snapshot_rejects_extra_files(tmp_path):
    directory = _snapshot(tmp_path)
    (directory / "extra.txt").write_text("unexpected")
    with pytest.raises(ContractError, match="unexpected adjustment snapshot files"):
        AdjustmentSnapshotReader(tmp_path).load("600000.XSHG", directory.name)
