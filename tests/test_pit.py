import pandas as pd
import pytest
from uq.errors import ContractError
from uq.store.pit import PitStore

def test_asof_excludes_future_and_selects_latest_revision():
    store = PitStore()
    store.append(pd.DataFrame([
        {"event_key":"f1","announcement_datetime":pd.Timestamp("2026-01-10"),"revision":1,"value":100,"source_event_id":"a"},
        {"event_key":"f1","announcement_datetime":pd.Timestamp("2026-02-01"),"revision":2,"value":120,"source_event_id":"b"},
    ]))
    assert store.read_asof(pd.Timestamp("2026-01-15")).iloc[0]["value"] == 100
    assert store.read_asof(pd.Timestamp("2026-02-02")).iloc[0]["value"] == 120
    assert store.read_asof(pd.Timestamp("2026-01-01")).empty

def test_duplicate_revision_is_rejected():
    store = PitStore()
    row = {"event_key":"x","announcement_datetime":pd.Timestamp("2026-01-01"),"revision":1,"value":1,"source_event_id":"a"}
    store.append(pd.DataFrame([row]))
    with pytest.raises(ContractError, match="duplicate PIT revision"):
        store.append(pd.DataFrame([row]))
