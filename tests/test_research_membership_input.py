import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import pytest

from tests.test_release_acceptance import ROOT, local_checks, quality_document
from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.index_membership import IndexMembershipStore, visible_from
from uq.errors import ContractError
from uq.factors.raw_price import calculate_raw_price_factors
from uq.factors.store import FactorStore, factor_generation
from uq.research_chain.membership_input import filtered_factor_frame, verify_request_input_sets

MEMBERS = ["600000.XSHG", "000001.XSHE", "600519.XSHG"]
CALENDAR = [
    value.date()
    for value in pd.bdate_range(start="2026-08-17", end="2026-10-30")
]
WINDOW_START = date(2026, 9, 7)
WINDOW_END = date(2026, 10, 12)
LAG = 2


def _publish_factor_day(root: Path, day: date):
    dates = pd.bdate_range(end=pd.Timestamp(day), periods=41)
    rows = [
        {
            "instrument": instrument,
            "datetime": value,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": float(index + 1),
            "amount": float(index + 1),
        }
        for index, value in enumerate(dates)
        for instrument in MEMBERS
    ]
    frame = calculate_raw_price_factors(pd.DataFrame(rows))
    frame = frame[frame["datetime"] == pd.Timestamp(day)].reset_index(drop=True)
    arguments = {
        "factor_set": "basic",
        "factor_version": "1.0.0",
        "frame": frame,
        "partition_date": day,
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime.fromisoformat(f"{day.isoformat()}T08:00:00+08:00"),
    }
    generation = factor_generation(**arguments)
    QualityReportStore().save(root, {**quality_document(generation), "checks": local_checks(arguments)})
    report = root / "reports" / "factor_v1" / generation / "report.json"
    arguments["quality_report_checksum"] = file_sha256_bytes(report.read_bytes())
    FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def _publish_snapshot(store: IndexMembershipStore, snapshot_date: date, instruments: list[str]):
    store.publish(
        universe_id="csi-all",
        index_code="000985.CSI",
        source="tushare:index_weight:000985.CSI",
        source_fetched_at=datetime(2026, 11, 2, tzinfo=timezone(timedelta(hours=8))),
        snapshot_date=snapshot_date,
        publish_lag_days=LAG,
        trading_days=CALENDAR,
        members=pd.DataFrame({"instrument": instruments}),
    )


def _fixture(tmp_path):
    factor_root = tmp_path / "factors"
    factor_root.mkdir(parents=True)
    membership_store = IndexMembershipStore(tmp_path / "membership")
    first_visible = visible_from(date(2026, 8, 31), LAG, CALENDAR)
    second_visible = visible_from(date(2026, 9, 30), LAG, CALENDAR)
    _publish_snapshot(membership_store, date(2026, 8, 31), ["600000.XSHG", "000001.XSHE"])
    _publish_snapshot(membership_store, date(2026, 9, 30), ["600000.XSHG", "600519.XSHG"])
    window_days = [day for day in CALENDAR if WINDOW_START <= day <= WINDOW_END]
    for day in window_days:
        _publish_factor_day(factor_root, day)
    factor_store = FactorStore(factor_root, FactorRegistry(ROOT))
    request = {
        "window_start_date": WINDOW_START.isoformat(),
        "window_end_date": WINDOW_END.isoformat(),
        "index_membership_binding": {
            "binding_type": "index_membership_set",
            "universe_id": "csi-all",
            "index_code": "000985.CSI",
            "window_start": WINDOW_START.isoformat(),
            "window_end": WINDOW_END.isoformat(),
            "set_digest_sha256": "",
        },
        "factor_partition_set_binding": {
            "binding_type": "factor_partition_set",
            "input_dataset": "bars_daily",
            "input_schema_version": "research-v1",
            "factor_set": "basic",
            "factor_version": "1.0.0",
            "window_start": WINDOW_START.isoformat(),
            "window_end": WINDOW_END.isoformat(),
            "set_digest_sha256": "",
        },
    }
    return request, membership_store, factor_store, window_days, first_visible, second_visible


def _bind(request, membership_store, factor_store):
    """Fill both set digests from the live stores, as a publisher would."""
    from uq.contracts.index_membership import build_membership_set
    from uq.factors.partition_set import build_factor_partition_set

    window_days = [day for day in CALENDAR if WINDOW_START <= day <= WINDOW_END]
    membership = build_membership_set(
        membership_store, universe_id="csi-all",
        window_start=WINDOW_START, window_end=WINDOW_END, trading_days=window_days,
    )
    partitions = build_factor_partition_set(
        factor_store, input_dataset="bars_daily", input_schema_version="research-v1",
        factor_set="basic", factor_version="1.0.0",
        window_start=WINDOW_START, window_end=WINDOW_END, trading_days=window_days,
    )
    request["index_membership_binding"]["set_digest_sha256"] = membership["set_digest_sha256"]
    request["factor_partition_set_binding"]["set_digest_sha256"] = partitions["set_digest_sha256"]
    return request


def test_window_filters_each_day_by_that_days_visible_membership(tmp_path):
    request, membership_store, factor_store, days, _first, second_visible = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    frame = filtered_factor_frame(
        request, membership_store=membership_store, factor_store=factor_store, trading_days=CALENDAR,
    )
    keyed = frame.groupby(frame["datetime"].dt.date)["instrument"].apply(set)
    assert set(keyed.index) == set(days)
    for day in days:
        if day < second_visible:
            assert keyed[day] == {"600000.XSHG", "000001.XSHE"}
        else:
            assert keyed[day] == {"600000.XSHG", "600519.XSHG"}


def test_snapshot_on_its_own_date_is_not_yet_visible(tmp_path):
    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    frame = filtered_factor_frame(
        request, membership_store=membership_store, factor_store=factor_store, trading_days=CALENDAR,
    )
    snapshot_day = frame[frame["datetime"] == pd.Timestamp("2026-09-30")]
    assert set(snapshot_day["instrument"]) == {"600000.XSHG", "000001.XSHE"}
    assert "600519.XSHG" not in set(snapshot_day["instrument"])


def test_tampered_members_bytes_reject_the_binding(tmp_path):
    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    members_path = next((tmp_path / "membership").rglob("members.csv"))
    members_path.write_text("instrument\n600000.XSHG\n")
    with pytest.raises(ContractError, match="tampered"):
        verify_request_input_sets(
            request, membership_store=membership_store, factor_store=factor_store, trading_days=CALENDAR,
        )


def test_membership_set_digest_drift_is_rejected(tmp_path):
    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    request["index_membership_binding"]["set_digest_sha256"] = "7" * 64
    with pytest.raises(ContractError, match="index membership set digest mismatch"):
        verify_request_input_sets(
            request, membership_store=membership_store, factor_store=factor_store, trading_days=CALENDAR,
        )


def test_factor_partition_set_digest_drift_is_rejected(tmp_path):
    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    request["factor_partition_set_binding"]["set_digest_sha256"] = "8" * 64
    with pytest.raises(ContractError, match="factor partition set digest mismatch"):
        verify_request_input_sets(
            request, membership_store=membership_store, factor_store=factor_store, trading_days=CALENDAR,
        )


def test_window_before_first_visible_snapshot_fails_closed(tmp_path):
    request, membership_store, factor_store, _days, first_visible, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    request["window_start_date"] = "2026-08-20"
    request["window_end_date"] = "2026-08-28"
    for field in ("index_membership_binding", "factor_partition_set_binding"):
        request[field]["window_start"] = "2026-08-20"
        request[field]["window_end"] = "2026-08-28"
    with pytest.raises(ContractError, match="no index membership visible"):
        verify_request_input_sets(
            request, membership_store=membership_store, factor_store=factor_store, trading_days=CALENDAR,
        )


def test_binding_window_must_match_the_research_window(tmp_path):
    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    request["index_membership_binding"]["window_end"] = "2026-10-30"
    with pytest.raises(ContractError, match="window does not match the research window"):
        verify_request_input_sets(
            request, membership_store=membership_store, factor_store=factor_store, trading_days=CALENDAR,
        )


def test_resolver_requires_both_membership_governance_inputs():
    from uq.research_chain.resolver import ResearchChainRequestResolver

    with pytest.raises(ContractError, match="membership store and a trading calendar"):
        ResearchChainRequestResolver({}, membership_store=object())
    with pytest.raises(ContractError, match="membership store and a trading calendar"):
        ResearchChainRequestResolver({}, trading_calendar=CALENDAR)


def test_v4_binding_without_membership_governance_fails_closed(tmp_path):
    from uq.research_chain.resolver import ResearchChainRequestResolver, ResearchResolutionError

    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    resolver = ResearchChainRequestResolver({"factor_partition": factor_store})
    with pytest.raises(ResearchResolutionError) as failure:
        resolver._verify_v4_input_sets(request)
    assert failure.value.reason == "request_invalid"


def test_v4_binding_reports_tampering_through_the_resolver(tmp_path):
    from uq.research_chain.resolver import ResearchChainRequestResolver, ResearchResolutionError

    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    request["factor_partition_set_binding"]["set_digest_sha256"] = "9" * 64
    resolver = ResearchChainRequestResolver(
        {"factor_partition": factor_store},
        membership_store=membership_store,
        trading_calendar=CALENDAR,
    )
    with pytest.raises(ResearchResolutionError) as failure:
        resolver._verify_v4_input_sets(request)
    assert failure.value.reason == "input_tampered"


def test_earlier_request_versions_resolve_without_membership_governance():
    from tests.test_research_chain_phase1 import (
        RecordingProvider,
        _request,
        _resolver,
        _upstream_manifests,
    )

    manifests = _upstream_manifests()
    resolver, _stores = _resolver(manifests)
    assert resolver.membership_store is None and resolver.trading_calendar is None
    plan = resolver.resolve(
        _request(manifests),
        quality_provider=RecordingProvider(),
        provider_config_ref="provider.json",
    )
    assert plan.request["contract_version"] != 4


def test_v5_contract_version_inputs_verify_the_membership_sets(tmp_path):
    # pendding 4c: v5 carries the same required set bindings as v4, so the
    # dispatch must run the PIT membership check for v5 too.  Before the fix the
    # dispatch was `request_schema == "research_run_request_v4"` only, and a v5
    # request would flow to result_reconciliation with unverified input sets.
    from uq.research_chain.resolver import ResearchChainRequestResolver, ResearchResolutionError

    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    resolver = ResearchChainRequestResolver(
        {"factor_partition": factor_store},
        membership_store=membership_store,
        trading_calendar=CALENDAR,
        market_dataset_store=SimpleNamespace(),
    )

    paper_checks: list[object] = []
    resolver._verify_v5_paper_bindings = paper_checks.append  # type: ignore[assignment]

    valid = _bind(dict(request), membership_store, factor_store)
    resolver._verify_contract_version_inputs("research_run_request_v5", valid)
    assert paper_checks == [valid]

    tampered = _bind(dict(request), membership_store, factor_store)
    tampered["factor_partition_set_binding"]["set_digest_sha256"] = "9" * 64
    with pytest.raises(ResearchResolutionError) as failure:
        resolver._verify_contract_version_inputs("research_run_request_v5", tampered)
    assert failure.value.reason == "input_tampered"


def test_v5_contract_version_inputs_require_membership_governance(tmp_path):
    from uq.research_chain.resolver import ResearchChainRequestResolver, ResearchResolutionError

    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    request = _bind(request, membership_store, factor_store)
    resolver = ResearchChainRequestResolver({"factor_partition": factor_store})
    with pytest.raises(ResearchResolutionError) as failure:
        resolver._verify_contract_version_inputs("research_run_request_v5", request)
    assert failure.value.reason == "request_invalid"
    assert "governed trading calendar" in failure.value.detail


def test_reconciliation_reverifies_input_sets_before_readback(tmp_path):
    # pendding 4c: the per-stage readback must re-bind the membership and factor
    # partition sets, so a store mutated after resolve-time verification cannot
    # publish a result whose input lineage no longer matches the request.
    from types import SimpleNamespace

    from uq.research_chain.runner import ResearchChainRunner

    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    bound = _bind(request, membership_store, factor_store)
    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    runner.factor_adapter = SimpleNamespace(store=factor_store)
    runner.dataset_adapter = SimpleNamespace(
        membership_store=membership_store, trading_calendar=CALENDAR
    )
    plan = SimpleNamespace(request={**bound, "contract_version": 5})
    runner._reverify_input_sets(plan)

    bound["factor_partition_set_binding"]["set_digest_sha256"] = "9" * 64
    with pytest.raises(ContractError, match="input set readback verification failed"):
        runner._reverify_input_sets(SimpleNamespace(request={**bound, "contract_version": 5}))


def test_reconciliation_skips_input_set_reverification_for_v1_and_needs_governance():
    from types import SimpleNamespace

    from uq.research_chain.runner import ResearchChainRunner
    from uq.errors import ContractError as _ContractError

    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    runner.dataset_adapter = SimpleNamespace()
    runner.factor_adapter = SimpleNamespace(store=object())
    runner._reverify_input_sets(SimpleNamespace(request={"contract_version": 1}))
    with pytest.raises(_ContractError, match="governed membership store and trading calendar"):
        runner._reverify_input_sets(SimpleNamespace(request={"contract_version": 5}))


def test_dataset_stage_adapter_consumes_the_membership_filtered_frame(tmp_path, monkeypatch):
    # pendding 4c: the dataset stage must read the per-day visible-member frame,
    # not the raw factor partition, whenever a governed membership store is wired.
    from uq.research_chain import adapters as adapters_module

    request, membership_store, factor_store, _days, _first, _second = _fixture(tmp_path)
    bound = _bind(request, membership_store, factor_store)
    adapter = adapters_module.DatasetStageAdapter.__new__(adapters_module.DatasetStageAdapter)
    adapter.factor_store = factor_store
    adapter.membership_store = membership_store
    adapter.trading_calendar = CALENDAR

    captured: dict[str, object] = {}

    def _fake_filtered(request, *, membership_store, factor_store, trading_days):
        captured["called"] = True
        from uq.research_chain.membership_input import filtered_factor_frame as real

        return real(
            request,
            membership_store=membership_store,
            factor_store=factor_store,
            trading_days=trading_days,
        )

    monkeypatch.setattr(adapters_module, "filtered_factor_frame", _fake_filtered)
    plan = SimpleNamespace(
        request={**bound, "contract_version": 5},
        stage_bindings=(),
    )
    frame = adapters_module.DatasetStageAdapter._read_factor_frame(adapter, plan, None)
    assert captured["called"] is True
    assert set(frame["instrument"]) <= {"600000.XSHG", "000001.XSHE", "600519.XSHG"}


def test_v4_v5_dataset_stage_requires_governed_membership(tmp_path):
    from types import SimpleNamespace

    from uq.research_chain import adapters as adapters_module

    adapter = adapters_module.DatasetStageAdapter.__new__(adapters_module.DatasetStageAdapter)
    adapter.membership_store = None
    adapter.trading_calendar = None
    adapter.factor_store = SimpleNamespace()
    plan = SimpleNamespace(request={"contract_version": 5}, stage_bindings=())
    with pytest.raises(ContractError, match="governed membership store"):
        adapters_module.DatasetStageAdapter._read_factor_frame(adapter, plan, None)


def test_governed_trading_days_come_from_the_published_calendar(tmp_path):
    # pendding 4c: the runtime calendar must be a governed artifact, not a
    # re-derived vendor calendar.  Overlapping calendar partitions contribute
    # sessions and a window with no covering snapshot fails closed.
    from uq.execution.market_dataset_publisher import MarketDatasetPublisher
    from uq.research_chain.cli import _governed_trading_days

    publisher = MarketDatasetPublisher(tmp_path / "market")
    sessions = [date(2025, 6, 3), date(2025, 6, 4), date(2025, 6, 5)]
    publisher.publish_calendar_snapshot(
        partition_date=date(2025, 6, 30),
        window_start=date(2025, 6, 1),
        window_end=date(2025, 6, 30),
        sessions=sessions,
        primary_source="tushare:trade_cal",
        independent_source="eastmoney_index_kline_v1",
    )
    days = _governed_trading_days(
        publisher, {"window_start_date": "2025-06-04", "window_end_date": "2025-06-30"}
    )
    assert days == [date(2025, 6, 4), date(2025, 6, 5)]

    with pytest.raises(ContractError, match="no governed trading calendar snapshot"):
        _governed_trading_days(
            publisher, {"window_start_date": "2025-08-01", "window_end_date": "2025-08-31"}
        )


def test_cli_build_runner_wires_membership_governance_into_the_dataset_stage(tmp_path):
    import importlib.util

    if importlib.util.find_spec("joblib") is None:
        pytest.skip("model runtime extras are unavailable")
    from uq.research_chain.cli import _build_runner
    from uq.research_chain.resolver import FileResearchRunStore

    run_store = FileResearchRunStore(tmp_path)
    store = IndexMembershipStore(tmp_path / "membership")
    run_store_cls = FileResearchRunStore
    runner = _build_runner(
        project_root=ROOT,
        data_root=tmp_path,
        run_store=run_store_cls(tmp_path),
        membership_store=store,
        trading_calendar=CALENDAR,
    )
    assert runner.dataset_adapter.membership_store is store
    assert runner.dataset_adapter.trading_calendar == CALENDAR
    assert run_store.root == tmp_path
