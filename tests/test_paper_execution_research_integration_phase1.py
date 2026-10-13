"""Phase 1 integration: v5 paper bindings resolve through the real resolver.

The v5 request pins its paper-execution template to four governed market-data
families.  These tests run the production ``ResearchChainRequestResolver``
against real stores: a ``MarketDatasetPublisher`` market store, a governed
``FactorStore`` with published partitions, and an ``IndexMembershipStore``.
Every check fails closed with a typed ``ResearchResolutionError`` reason.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.gate_contracts import canonical_json, sha256_bytes
from uq.contracts.index_membership import IndexMembershipStore, build_membership_set
from uq.contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities,
    research_stage_plan_v5_sha256,
)
from uq.errors import ContractError
from uq.execution.market_dataset_publisher import MarketDatasetPublisher
from uq.factors.partition_set import build_factor_partition_set
from uq.factors.raw_price import calculate_raw_price_factors
from uq.factors.store import FactorStore, factor_generation
from uq.research_chain.resolver import (
    ResearchChainRequestResolver,
    ResearchResolutionError,
    stage_plan_for_request,
)
from tests.test_release_acceptance import local_checks, quality_document
from tests.test_research_chain_phase1 import (
    FakeOwningStore,
    RecordingProvider,
    _upstream_manifests,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evidence/paper-execution-research-integration/phase-0r/fixtures/research_run_request_v5-valid.json"

WINDOW_START = date(2025, 12, 1)
WINDOW_END = date(2026, 1, 29)
# A sparse but fully governed world: every window day is a session with
# published calendar/bars/suspension/corporate-action partitions, a factor
# partition, and visible index membership.  The window endpoints are sessions
# so the set-binding windows equal the request window exactly.
PRE_WINDOW_DAY = date(2025, 11, 28)
WINDOW_DAYS = [date(2025, 12, 1), date(2025, 12, 30), date(2026, 1, 29)]
CALENDAR = [PRE_WINDOW_DAY, *WINDOW_DAYS]
MEMBERS = ["600000.XSHG", "000001.XSHE", "600519.XSHG"]

_PAPER_FIELDS = {
    "market_data_binding": "bars_daily_v2",
    "calendar_binding": "trading_calendar_snapshot_v1",
    "suspension_binding": "suspension_snapshot_v1",
    "corporate_action_binding": "dividend_event_v1",
}


class _EchoFactorProvider(RecordingProvider):
    """RecordingProvider that echoes the real factor subject generation.

    The shared v1 harness hardcodes the factor subject (its factor manifest is
    the fake ``"a" * 64``); the full-resolve lock binds a real published factor
    partition, so the decision — including the embedded owning report and its
    checksum — must name the real subject generation.
    """

    def resolve(self, *, binding_type, subject_generation_id, subject_manifest_digest_sha256, **kwargs):
        decision = dict(super().resolve(
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_manifest_digest_sha256=subject_manifest_digest_sha256,
            **kwargs,
        ))
        if binding_type == "factor_v1":
            report = dict(decision["owning_report"])
            report["bound_generation_id"] = subject_generation_id
            decision["owning_report"] = report
            decision["subject_generation_id"] = subject_generation_id
            decision["decision_checksum_sha256"] = sha256_bytes(canonical_json(report))
        return decision


def _publish_factor_day(factor_root: Path, day: date) -> str:
    """Publish one governed basic/research-v1 factor partition; return its generation."""
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
    QualityReportStore().save(factor_root, {**quality_document(generation), "checks": local_checks(arguments)})
    report = factor_root / "reports" / "factor_v1" / generation / "report.json"
    arguments["quality_report_checksum"] = file_sha256_bytes(report.read_bytes())
    FactorStore(factor_root, FactorRegistry(ROOT)).publish(**arguments)
    return generation


def _publish_paper_inputs(store: MarketDatasetPublisher, staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    store.publish_calendar_snapshot(
        partition_date=WINDOW_END,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        sessions=WINDOW_DAYS,
        primary_source="tushare:trade_cal",
        independent_source="eastmoney_index_kline_v1",
    )
    for session in WINDOW_DAYS:
        bars = pd.DataFrame({
            "instrument": ["000001.XSHE"],
            "session_date": [session.isoformat()],
            "open": [10.2],
            "high": [10.4],
            "low": [10.1],
            "close": [10.0],
            "volume": [1_000_000.0],
            "amount": [1.02e7],
            "status": ["trading"],
            "limit_up": [11.0],
            "limit_down": [9.0],
            "adj_factor": [1.0],
        })
        source = staging / f"bars-{session.isoformat()}.parquet"
        bars.to_parquet(source, index=False)
        store.publish_bars_partition(
            partition_date=session,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            source_data_path=source,
            source_generation_id=hashlib.sha256(f"bars-{session.isoformat()}".encode()).hexdigest(),
            source_data_checksum_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            columns=list(bars.columns),
            row_count=len(bars),
        )
        store.publish_suspension_snapshot(
            partition_date=session,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            frame=pd.DataFrame({"instrument": [], "session_date": [], "status": []}),
        )
        store.publish_corporate_action_snapshot(
            partition_date=session,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            frame=pd.DataFrame({"date": [], "instrument": []}),
            source_generation_ids=[],
            event_count=0,
        )


def _bind_paper_families(request: dict, store: MarketDatasetPublisher) -> dict:
    template = request["paper_execution_template"]
    for field, family in _PAPER_FIELDS.items():
        template[field] = store.build_set_binding(
            family, window_start=WINDOW_START, window_end=WINDOW_END
        )
    return request


def _paper_world(tmp_path: Path) -> tuple[dict, MarketDatasetPublisher]:
    """The v5 fixture with its four paper bindings built from a real market store."""
    store = MarketDatasetPublisher(tmp_path / "market-store")
    _publish_paper_inputs(store, tmp_path / "staging")
    request = json.loads(FIXTURE.read_text())
    return _bind_paper_families(request, store), store


def _world(tmp_path: Path) -> tuple[dict, ResearchChainRequestResolver]:
    """A complete governed world in which the v5 fixture resolves end to end."""
    request, market_store = _paper_world(tmp_path)

    factor_root = tmp_path / "factors"
    factor_root.mkdir(parents=True)
    generations = {day: _publish_factor_day(factor_root, day) for day in WINDOW_DAYS}
    factor_store = FactorStore(factor_root, FactorRegistry(ROOT))
    factor_manifest = factor_store.read_manifest(generations[WINDOW_DAYS[0]])

    membership_store = IndexMembershipStore(tmp_path / "membership")
    membership_store.publish(
        universe_id="csi-all",
        index_code="000985.CSI",
        source="tushare:index_weight:000985.CSI",
        source_fetched_at=datetime(2025, 11, 28, tzinfo=timezone(timedelta(hours=8))),
        snapshot_date=PRE_WINDOW_DAY,
        publish_lag_days=0,
        trading_days=CALENDAR,
        members=pd.DataFrame({"instrument": MEMBERS}),
    )

    manifests = _upstream_manifests()
    request["factor_binding"].update({
        "generation_id": factor_manifest["generation_id"],
        "manifest_digest_sha256": factor_manifest["manifest_digest_sha256"],
    })
    request["universe_snapshot_binding"].update({
        "generation_id": manifests["universe_snapshot"]["generation_id"],
        "manifest_digest_sha256": manifests["universe_snapshot"]["manifest_digest_sha256"],
    })
    request["adjusted_price_binding"].update({
        "generation_id": manifests["adjusted_price_dataset"]["generation_id"],
        "data_checksum_sha256": manifests["adjusted_price_dataset"]["data_checksum_sha256"],
    })
    request["label_binding"].update({
        "generation_id": manifests["label_set"]["generation_id"],
        "manifest_digest_sha256": manifests["label_set"]["manifest_digest_sha256"],
    })
    request["feature_preprocessing_binding"].update({
        "generation_id": manifests["feature_preprocessing"]["generation_id"],
        "manifest_digest_sha256": manifests["feature_preprocessing"]["manifest_digest_sha256"],
    })
    request["backtest_config_binding"].update({
        "generation_id": manifests["backtest_config"]["generation_id"],
        "manifest_digest_sha256": manifests["backtest_config"]["manifest_digest_sha256"],
    })
    request["index_membership_binding"].update({
        "universe_id": "csi-all",
        "index_code": "000985.CSI",
        "set_digest_sha256": build_membership_set(
            membership_store,
            universe_id="csi-all",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            trading_days=WINDOW_DAYS,
        )["set_digest_sha256"],
    })
    request["factor_partition_set_binding"].update({
        "set_digest_sha256": build_factor_partition_set(
            factor_store,
            input_dataset="bars_daily",
            input_schema_version="research-v1",
            factor_set="basic",
            factor_version="1.0.0",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            trading_days=WINDOW_DAYS,
        )["set_digest_sha256"],
    })
    generation, digest = research_contract_identities(request, schema_name="research_run_request_v5")
    request["request_content_generation_id"] = generation
    request["manifest_digest_sha256"] = digest

    stores = {
        "factor_partition": factor_store,
        "universe_snapshot": FakeOwningStore(manifests["universe_snapshot"]),
        "adjusted_price_dataset": FakeOwningStore(manifests["adjusted_price_dataset"]),
        "label_set": FakeOwningStore(manifests["label_set"]),
        "feature_preprocessing": FakeOwningStore(manifests["feature_preprocessing"]),
        "backtest_config": FakeOwningStore(manifests["backtest_config"]),
    }
    resolver = ResearchChainRequestResolver(
        stores,
        membership_store=membership_store,
        trading_calendar=CALENDAR,
        market_dataset_store=market_store,
    )
    return request, resolver


def test_v5_request_resolves_end_to_end_through_real_stores(tmp_path: Path) -> None:
    """Regression lock: the real resolve() drives the whole v5 path to a plan."""
    request, resolver = _world(tmp_path)
    plan = resolver.resolve(
        request, quality_provider=_EchoFactorProvider(), provider_config_ref="test-provider"
    )
    assert plan.request["contract_version"] == 5
    assert plan.stage_plan_sha256 == research_stage_plan_v5_sha256()
    assert [binding.output_family for binding in plan.stage_bindings] == [
        "factor_partition",
        "universe_snapshot",
        "adjusted_price_dataset",
        "label_set",
        "feature_preprocessing",
        "backtest_config",
    ]


def test_v5_resolve_without_stores_fails_closed_with_typed_reason() -> None:
    """resolve() reaches the owning-store loop (no NameError) and fails typed."""
    request = json.loads(FIXTURE.read_text())
    resolver = ResearchChainRequestResolver({})
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver.resolve(request)
    assert excinfo.value.reason == "request_invalid"
    assert "no owning store is registered for output family factor_partition" in str(excinfo.value)


def test_paper_binding_family_is_enforced_fail_closed(tmp_path: Path) -> None:
    """M1: a binding naming the wrong family is a lineage break, not unresolved input."""
    request, store = _paper_world(tmp_path)
    template = request["paper_execution_template"]
    template["market_data_binding"] = store.build_set_binding(
        "suspension_snapshot_v1", window_start=WINDOW_START, window_end=WINDOW_END
    )
    resolver = ResearchChainRequestResolver({}, market_dataset_store=store)
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver._verify_v5_paper_bindings(request)
    assert excinfo.value.reason == "lineage_mismatch"
    assert "market_data_binding must name family bars_daily_v2" in str(excinfo.value)
    # The template schema pins each binding field's family as a per-field const.
    with pytest.raises(ContractError):
        ModelContractLoader.validate("paper_execution_template_v2", template)


def test_paper_binding_window_must_cover_the_research_window(tmp_path: Path) -> None:
    """M2: a binding that pins a window not covering the request fails closed."""
    request, store = _paper_world(tmp_path)
    template = request["paper_execution_template"]
    binding = dict(template["calendar_binding"])
    binding["window_end"] = "2026-01-15"
    template["calendar_binding"] = binding
    resolver = ResearchChainRequestResolver({}, market_dataset_store=store)
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver._verify_v5_paper_bindings(request)
    assert excinfo.value.reason == "input_unresolved"
    assert "does not cover the research window" in str(excinfo.value)


def test_missing_paper_binding_fails_closed(tmp_path: Path) -> None:
    request, store = _paper_world(tmp_path)
    del request["paper_execution_template"]["market_data_binding"]
    resolver = ResearchChainRequestResolver({}, market_dataset_store=store)
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver._verify_v5_paper_bindings(request)
    assert excinfo.value.reason == "input_unresolved"
    assert "missing paper binding: market_data_binding" in str(excinfo.value)


def test_tampered_paper_input_fails_closed_before_compute(tmp_path: Path) -> None:
    request, store = _paper_world(tmp_path)
    request["paper_execution_template"]["suspension_binding"]["set_digest_sha256"] = "0" * 64
    resolver = ResearchChainRequestResolver({}, market_dataset_store=store)
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver._verify_v5_paper_bindings(request)
    assert excinfo.value.reason == "input_tampered"
    assert "partition set digest mismatch" in str(excinfo.value)


def test_v5_without_market_store_fails_closed(tmp_path: Path) -> None:
    request, _store = _paper_world(tmp_path)
    resolver = ResearchChainRequestResolver({})
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver._verify_v5_paper_bindings(request)
    assert excinfo.value.reason == "request_invalid"
    assert "requires a market_dataset_store" in str(excinfo.value)


def test_v1_v2_resolver_behaviour_unchanged(tmp_path: Path) -> None:
    resolver = ResearchChainRequestResolver({})
    assert stage_plan_for_request({"contract_version": 1}) == stage_plan_for_request({"contract_version": 2})
    assert "paper_execution" not in stage_plan_for_request({"contract_version": 1})


def test_v5_resolve_with_a_shape_valid_non_date_window_fails_typed(tmp_path: Path) -> None:
    """A "2026-99-99" passes the schema's shape-only pattern; resolve must fail
    with a typed resolution error, not a bare ValueError the CLI would degrade
    to configuration_failure."""
    request, resolver = _world(tmp_path)
    request["window_start_date"] = "2026-99-99"
    generation, digest = research_contract_identities(request, schema_name="research_run_request_v5")
    request["request_content_generation_id"] = generation
    request["manifest_digest_sha256"] = digest
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver.resolve(
            request, quality_provider=_EchoFactorProvider(), provider_config_ref="test-provider"
        )
    assert excinfo.value.reason == "input_unresolved"
    assert "not a real calendar date" in str(excinfo.value)


def test_paper_binding_with_a_shape_valid_non_date_window_fails_typed(tmp_path: Path) -> None:
    """The same shape-only gap on a paper binding window fails typed."""
    request, store = _paper_world(tmp_path)
    binding = dict(request["paper_execution_template"]["calendar_binding"])
    binding["window_start"] = "2026-99-99"
    request["paper_execution_template"]["calendar_binding"] = binding
    resolver = ResearchChainRequestResolver({}, market_dataset_store=store)
    with pytest.raises(ResearchResolutionError) as excinfo:
        resolver._verify_v5_paper_bindings(request)
    assert excinfo.value.reason == "input_unresolved"
    assert "calendar_binding window date is not a real calendar date" in str(excinfo.value)
