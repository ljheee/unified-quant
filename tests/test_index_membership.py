from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.gate_contracts import adjustment_snapshot_generation
from uq.contracts.index_membership import IndexMembershipStore, visible_from
from uq.errors import ContractError

CALENDAR = (
    [date(2025, 9, d) for d in range(1, 31)]
    + [date(2025, 10, 8), date(2025, 10, 9), date(2025, 10, 10), date(2025, 10, 13), date(2025, 10, 14)]
    + [date(2025, 10, d) for d in (15, 16, 17, 20, 21, 22, 23, 24, 27, 28, 29, 30, 31)]
)


def _members(*codes):
    return pd.DataFrame({"instrument": list(codes)})


def _publish(store, snapshot_date, codes, lag=2):
    return store.publish(
        universe_id="a500",
        index_code="000510.SH",
        source="tushare:index_weight",
        source_fetched_at=datetime(2025, 10, 10, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        snapshot_date=snapshot_date,
        publish_lag_days=lag,
        trading_days=CALENDAR,
        members=_members(*codes),
    )


def test_visible_from_counts_trading_days_after_snapshot():
    # Spec: usable from the first trading day after snapshot_date, plus publish_lag_days.
    # With lag 2 that is the third trading day after 2025-09-30 (10-08, 10-09, 10-10).
    assert visible_from(date(2025, 9, 30), 2, CALENDAR) == date(2025, 10, 10)


def test_visible_from_rejects_negative_lag_and_short_calendar():
    with pytest.raises(ContractError):
        visible_from(date(2025, 9, 30), -1, CALENDAR)
    with pytest.raises(ContractError):
        visible_from(date(2025, 10, 30), 5, CALENDAR)


def test_snapshot_is_not_visible_before_its_visible_from(tmp_path):
    store = IndexMembershipStore(tmp_path)
    _publish(store, date(2025, 9, 30), ["000001.XSHE"], lag=2)
    with pytest.raises(ContractError, match="no index membership visible"):
        store.as_of("a500", date(2025, 10, 9))
    selected, members = store.as_of("a500", date(2025, 10, 10))
    assert selected == date(2025, 9, 30)
    assert members["instrument"].tolist() == ["000001.XSHE"]


def test_as_of_selects_latest_visible_snapshot(tmp_path):
    store = IndexMembershipStore(tmp_path)
    _publish(store, date(2025, 9, 30), ["000001.XSHE"], lag=1)
    _publish(store, date(2025, 10, 15), ["000002.XSHE"], lag=1)
    assert store.as_of("a500", date(2025, 10, 10))[0] == date(2025, 9, 30)
    assert store.as_of("a500", date(2025, 10, 20))[0] == date(2025, 10, 15)


def test_publish_is_immutable(tmp_path):
    store = IndexMembershipStore(tmp_path)
    _publish(store, date(2025, 9, 30), ["000001.XSHE"])
    with pytest.raises(ContractError, match="immutable"):
        _publish(store, date(2025, 9, 30), ["000002.XSHE"])


def test_tampered_members_bytes_are_rejected(tmp_path):
    store = IndexMembershipStore(tmp_path)
    directory = _publish(store, date(2025, 9, 30), ["000001.XSHE"])
    (directory / "members.csv").write_text("instrument\n000009.XSHE\n")
    with pytest.raises(ContractError, match="tampered"):
        store.read_members("a500", date(2025, 9, 30))


def test_members_must_be_instrument_only_and_unique(tmp_path):
    store = IndexMembershipStore(tmp_path)
    with pytest.raises(ContractError, match="exactly instrument"):
        store.publish(
            universe_id="a500", index_code="000510.SH", source="x",
            source_fetched_at=datetime(2025, 10, 10, tzinfo=timezone(timedelta(hours=8))), snapshot_date=date(2025, 9, 30),
            publish_lag_days=0, trading_days=CALENDAR,
            members=pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [0.1]}),
        )
    with pytest.raises(ContractError, match="duplicate"):
        _publish(store, date(2025, 9, 30), ["000001.XSHE", "000001.XSHE"])


def test_manifest_generation_is_validated_on_read(tmp_path):
    store = IndexMembershipStore(tmp_path)
    directory = _publish(store, date(2025, 9, 30), ["000001.XSHE"])
    import json
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["source"] = "forged"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError):
        store.read_manifest("a500", date(2025, 9, 30))


def test_naive_source_fetched_at_is_rejected(tmp_path):
    store = IndexMembershipStore(tmp_path)
    with pytest.raises(ContractError, match="timezone-aware"):
        store.publish(
            universe_id="a500", index_code="000510.SH", source="x",
            source_fetched_at=datetime(2025, 10, 10, 9, 0), snapshot_date=date(2025, 9, 30),
            publish_lag_days=0, trading_days=CALENDAR, members=_members("000001.XSHE"),
        )


def test_invalid_calendar_dates_are_rejected_on_read(tmp_path):
    import json
    store = IndexMembershipStore(tmp_path)
    directory = _publish(store, date(2025, 9, 30), ["000001.XSHE"])
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["visible_from"] = "2025-02-31"
    manifest["generation_id"] = adjustment_snapshot_generation(
        {key: value for key, value in manifest.items() if key != "generation_id"}
    )
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match="invalid calendar date"):
        store.read_manifest("a500", date(2025, 9, 30))


@pytest.mark.parametrize("name", [".", "..", "sub/members.csv", "../members.csv"])
def test_members_artifact_path_must_be_plain_filename(name):
    from uq.contracts.gate_contracts import validate_contract

    base = {
        "snapshot_version": 1, "universe_id": "a500", "index_code": "000510.SH", "source": "x",
        "source_fetched_at": "2025-10-10T09:00:00+08:00", "snapshot_date": "2025-09-30",
        "visible_from": "2025-10-10", "publish_lag_days": 0, "pit_precision": "monthly_approximation",
        "members_artifact": {"path": name, "row_count": 1, "checksum_sha256": "a" * 64},
        "generation_id": "b" * 64,
    }
    with pytest.raises(ContractError):
        validate_contract("index_membership_snapshot.v1.json", base)


def test_empty_month_produces_no_snapshot():
    import importlib.util
    from pathlib import Path as _Path

    spec = importlib.util.spec_from_file_location(
        "fetch_index_membership", _Path(__file__).resolve().parents[1] / "scripts" / "fetch_index_membership.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.group_snapshots(pd.DataFrame()) == {}


def _membership_fixture(tmp_path):
    from datetime import timedelta

    from uq.contracts.index_membership import build_membership_set

    calendar = (
        [date(2025, 9, d) for d in range(1, 31) if date(2025, 9, d).weekday() < 5]
        + [date(2025, 10, d) for d in range(1, 32) if date(2025, 10, d).weekday() < 5]
        + [date(2025, 11, d) for d in range(3, 31) if date(2025, 11, d).weekday() < 5]
    )
    store = IndexMembershipStore(tmp_path)
    tz = timezone(timedelta(hours=8))
    for snapshot_date, codes in ((date(2025, 9, 30), ["000001.XSHE", "000002.XSHE"]),
                                 (date(2025, 10, 31), ["000002.XSHE", "600000.XSHG"])):
        store.publish(
            universe_id="a500", index_code="000510.SH", source="x",
            source_fetched_at=datetime(2025, 11, 30, tzinfo=tz), snapshot_date=snapshot_date,
            publish_lag_days=2, trading_days=calendar, members=_members(*codes),
        )
    document = build_membership_set(
        store, universe_id="a500", window_start=date(2025, 10, 10),
        window_end=date(2025, 11, 28), trading_days=calendar,
    )
    return store, calendar, document


def test_membership_set_selects_snapshots_by_visibility(tmp_path):
    store, calendar, document = _membership_fixture(tmp_path)
    assert [entry["snapshot_date"] for entry in document["snapshots"]] == ["2025-09-30", "2025-10-31"]
    assert [entry["visible_from"] for entry in document["snapshots"]] == ["2025-10-03", "2025-11-05"]
    from uq.contracts.index_membership import verify_membership_set
    verify_membership_set(store, document, calendar)


def test_earliest_visible_date_reports_first_usable_start(tmp_path):
    store, _, _ = _membership_fixture(tmp_path)
    assert store.earliest_visible_date("a500") == date(2025, 10, 3)
    with pytest.raises(ContractError, match="earliest visible date is 2025-10-03"):
        store.as_of("a500", date(2025, 10, 2))


def test_membership_set_rejects_window_before_first_visible_snapshot(tmp_path):
    from uq.contracts.index_membership import build_membership_set

    store, calendar, _ = _membership_fixture(tmp_path)
    with pytest.raises(ContractError, match="no index membership visible"):
        build_membership_set(
            store, universe_id="a500", window_start=date(2025, 9, 2),
            window_end=date(2025, 9, 5), trading_days=calendar,
        )


def test_membership_set_rejects_tampered_window(tmp_path):
    import json
    from uq.contracts.index_membership import verify_membership_set

    store, calendar, document = _membership_fixture(tmp_path)
    tampered = json.loads(json.dumps(document))
    tampered["window_end"] = "2025-10-30"
    with pytest.raises(ContractError, match="digest mismatch"):
        verify_membership_set(store, tampered, calendar)


def test_membership_set_rejects_stale_snapshot_even_with_resigned_digest(tmp_path):
    import json
    from uq.contracts.gate_contracts import sha256_json
    from uq.contracts.index_membership import verify_membership_set

    store, calendar, document = _membership_fixture(tmp_path)
    forged = json.loads(json.dumps(document))
    forged["snapshots"] = forged["snapshots"][:1]
    forged["set_digest_sha256"] = sha256_json(
        {key: value for key, value in forged.items() if key != "set_digest_sha256"}
    )
    with pytest.raises(ContractError, match="not latest visible"):
        verify_membership_set(store, forged, calendar)


def test_membership_set_rejects_forged_generation(tmp_path):
    import json
    from uq.contracts.gate_contracts import sha256_json
    from uq.contracts.index_membership import verify_membership_set

    store, calendar, document = _membership_fixture(tmp_path)
    forged = json.loads(json.dumps(document))
    forged["snapshots"][0]["generation_id"] = "0" * 64
    forged["set_digest_sha256"] = sha256_json(
        {key: value for key, value in forged.items() if key != "set_digest_sha256"}
    )
    with pytest.raises(ContractError, match="generation mismatch"):
        verify_membership_set(store, forged, calendar)


def test_beijing_exchange_instruments_are_accepted_and_round_trip():
    from uq.adapters.tushare_free import _to_instrument, _to_provider_code
    from uq.contracts.gate_contracts import validate_contract

    assert _to_instrument("920438.BJ") == "920438.XBSE"
    assert _to_provider_code("920438.XBSE") == "920438.BJ"
    assert _to_instrument("600000.SH") == "600000.XSHG"
    assert _to_provider_code("000001.XSHE") == "000001.SZ"
    validate_contract("index_membership_row.v1.json", {"instrument": "920438.XBSE"})
    with pytest.raises(ValueError):
        _to_instrument("920438.HK")
    with pytest.raises(ValueError):
        _to_provider_code("920438.XFOO")


def test_beijing_instrument_missing_is_fail_closed_without_beijing_roster():
    from uq.market.lifecycle import AkShareLifecycleProvider

    class _Lifecycle(AkShareLifecycleProvider):
        def __init__(self) -> None:
            pass

        def _listed_codes(self):
            return {"600000"}

        def _delisted_codes(self):
            return {}

    result = _Lifecycle().classify_missing(date(2026, 8, 21), ["920438.XBSE"], ("akshare",))
    assert result == {"920438.XBSE": "unknown_requires_review"}


def test_v4_stage_plan_review_signature_chain_verifies_and_rejects_tamper(tmp_path):
    import json
    import subprocess
    import sys
    from uq.contracts.model_layer import research_stage_plan_v4_sha256
    from uq.research_chain.contracts import verify_stage_plan_review

    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "review.json"
    subprocess.run(
        [sys.executable, str(root / "scripts" / "sign_stage_plan_review.py"), "--output", str(output)],
        check=True, cwd=root,
    )
    review = json.loads(output.read_text())
    stage_plan = research_stage_plan_v4_sha256()
    verify_stage_plan_review(review, stage_plan_sha256=stage_plan)
    with pytest.raises(ContractError, match="signature mismatch"):
        verify_stage_plan_review({**review, "reviewer": "someone-else"}, stage_plan_sha256=stage_plan)


def test_v4_request_schema_is_registered_but_runtime_stays_disabled():
    import json
    from jsonschema import Draft202012Validator
    from uq.research_chain.contracts import research_request_schema_name

    assert research_request_schema_name({"contract_version": 4}) == "research_run_request_v4"
    schema = json.loads((Path(__file__).resolve().parents[1] / "config/schemas/contracts/research_run_request_v4.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert "index_membership_binding" in schema["required"]
    assert schema["properties"]["stage_plan_review"]["properties"]["review_type"]["const"] == "research_stage_plan_v4_activation"
