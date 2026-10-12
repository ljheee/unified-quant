"""bars_daily v2 ingest: session_date key, Tushare primary, TDX comparator."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from uq.adapters.tushare import TushareAdapter
from uq.contracts.config import load_dataset_contract
from uq.contracts.index_membership import IndexMembershipStore
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.pipeline.daily import DailyIngestPipeline, IngestReport, build_tushare_v2_pipeline
from uq.routing.router import SourceRouter
from uq.sources.fetch import FetchResult, FetchStatus
from tests.test_real_chain import FakeMootdxAdapter, mootdx_page
from tests.test_tushare_v2_adapter import V2_FIELDS, _Client, _adapter

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config/schemas/bars_daily.v2.yaml"
CONTRACT_PATH = ROOT / "config/datasets/bars_daily.v2.yaml"
TRADE_DATE = date(2026, 3, 2)
INSTRUMENTS = ["600000.XSHG", "000001.XSHE"]


def _pipeline(root: Path, adapters: dict, **overrides) -> DailyIngestPipeline:
    contract = load_dataset_contract(CONTRACT_PATH, load_schema(SCHEMA_PATH))
    options = {"run_health_probe": False}
    options.update(overrides)
    return DailyIngestPipeline(
        root,
        SCHEMA_PATH,
        CONTRACT_PATH,
        SourceRouter(contract, adapters),
        **options,
    )


def _v2_adapters() -> dict:
    return {"tushare": _adapter(_Client()), "tdx": FakeMootdxAdapter(mootdx_page())}


def test_session_key_comes_from_the_schema_primary_key():
    pipeline = _pipeline(Path("/tmp/unused-v2-key"), {"tushare": _adapter(_Client())})
    assert pipeline.session_key == "session_date"


def test_research_v1_pipeline_keeps_the_datetime_key(tmp_path):
    schema = ROOT / "config/schemas/bars_daily.research-v1.yaml"
    contract = ROOT / "config/datasets/bars_daily.research-v1.yaml"
    loaded = load_dataset_contract(contract, load_schema(schema))
    pipeline = DailyIngestPipeline(
        tmp_path,
        schema,
        contract,
        SourceRouter(loaded, {"tdx": FakeMootdxAdapter(mootdx_page())}),
        run_health_probe=False,
    )
    assert pipeline.session_key == "datetime"


def test_primary_key_without_exactly_one_session_field_is_rejected(tmp_path):
    # The pipeline derives the merge/verified-row key from the schema; a key
    # that is not instrument-plus-one-session-field must trip at construction
    # rather than silently merge on the wrong column.
    broken = tmp_path / "broken-primary-key.yaml"
    broken.write_text(
        SCHEMA_PATH.read_text().replace(
            "primary_key:\n  - instrument\n  - session_date\n",
            "primary_key:\n  - instrument\n  - session_date\n  - open\n",
        )
    )
    assert "  - open" in broken.read_text().split("fields:")[0]
    router = SourceRouter(
        load_dataset_contract(CONTRACT_PATH, load_schema(broken)),
        {"tushare": _adapter(_Client())},
    )
    with pytest.raises(ContractError, match="one session field"):
        DailyIngestPipeline(tmp_path, broken, CONTRACT_PATH, router, run_health_probe=False)


def test_builders_default_to_the_repository_root(tmp_path):
    # daily.py sits three levels below the repo root, so the historical
    # parents[2] default pointed at <repo>/src; only the CLI's explicit
    # --project-root kept the shipped builders usable.
    pipeline = build_tushare_v2_pipeline(tmp_path, run_health_probe=False)
    assert pipeline.schema.version == "v2"
    assert pipeline.contract.primary_source == "tushare"


def test_v2_publishes_from_tushare_with_reference_fields(tmp_path):
    report = _pipeline(tmp_path, _v2_adapters()).run(TRADE_DATE, INSTRUMENTS)
    assert report.status == "published", report.errors
    assert report.rows == 2
    published = pd.read_parquet(report.published_path)
    assert {"session_date", "status", "adj_factor", "limit_up"} <= set(published.columns)
    assert set(published["instrument"]) == set(INSTRUMENTS)


def test_comparator_without_the_session_key_is_rejected(tmp_path):
    class V1OnlyComparator:
        source_name = "tdx"

        def fetch(self, dataset, instruments, start, end, fields):
            frame = pd.DataFrame(
                {
                    "instrument": ["600000.XSHG"],
                    "datetime": pd.to_datetime(["2026-03-02"]),
                    "close": [10.2],
                    "volume": [10000.0],
                }
            )
            # Envelope declares v2, so the router's envelope check passes;
            # only the key-column guard notices the rows are still keyed on
            # datetime.
            return FetchResult(
                frame,
                FetchStatus.SUCCESS,
                self.source_name,
                dataset,
                "v2",
                frozenset(fields),
                frozenset(fields) & set(frame.columns),
                datetime.now(),
            )

    adapters = {"tushare": _adapter(_Client()), "tdx": V1OnlyComparator()}
    report = _pipeline(tmp_path, adapters).run(TRADE_DATE, INSTRUMENTS)
    assert report.status == "comparator_key_missing"
    assert any("lack the dataset key columns" in error for error in report.errors)


def test_comparator_on_the_wrong_schema_version_fails_closed(tmp_path):
    class V1Comparator:
        source_name = "tdx"

        def fetch(self, dataset, instruments, start, end, fields):
            frame = pd.DataFrame(
                {
                    "instrument": ["600000.XSHG"],
                    "datetime": pd.to_datetime(["2026-03-02"]),
                    "close": [10.2],
                    "volume": [10000.0],
                }
            )
            return FetchResult(
                frame,
                FetchStatus.SUCCESS,
                self.source_name,
                dataset,
                "research-v1",
                frozenset(fields),
                frozenset(fields) & set(frame.columns),
                datetime.now(),
            )

    adapters = {"tushare": _adapter(_Client()), "tdx": V1Comparator()}
    report = _pipeline(tmp_path, adapters).run(TRADE_DATE, INSTRUMENTS)
    assert report.status == "primary_source_unsupported_request"
    assert any("tdx envelope mismatch" in error for error in report.errors)


def test_mootdx_serves_the_requested_session_key():
    adapter = FakeMootdxAdapter(mootdx_page())
    v2 = adapter.fetch("bars_daily", ["600000.XSHG"], "2026-08-21", "2026-08-21", V2_FIELDS)
    assert v2.schema_version == "v2"
    assert "session_date" in v2.rows.columns
    assert "datetime" not in v2.rows.columns
    assert v2.observed_start == date(2026, 8, 21)

    v1 = adapter.fetch("bars_daily", ["600000.XSHG"], "2026-08-21", "2026-08-21", ["instrument", "datetime", "close"])
    assert v1.schema_version == "research-v1"
    assert "datetime" in v1.rows.columns


def test_v2_contract_only_claims_fields_tdx_can_serve():
    contract = load_dataset_contract(CONTRACT_PATH, load_schema(SCHEMA_PATH))
    tdx = contract.sources["tdx"].provides
    assert "status" not in tdx and "adj_factor" not in tdx
    # tdx therefore cannot be primary for the v2 required-field set
    eligible = {item.source_name for item in contract.eligible_sources(set(contract.required_fields))}
    assert eligible == {"tushare"}


def test_health_probe_reports_real_upstream_state():
    class OkClient:
        def trade_cal(self, exchange, start_date, end_date):
            return pd.DataFrame({"cal_date": ["20260302"], "is_open": ["1"]})

    class BrokenClient:
        def trade_cal(self, exchange, start_date, end_date):
            raise Exception("抱歉，您没有接口(trade_cal)访问权限")

    healthy = _adapter(OkClient()).health_probe()
    assert healthy["ok"] is True

    unhealthy = _adapter(BrokenClient()).health_probe()
    assert unhealthy["ok"] is False
    assert "trade_cal" in str(unhealthy["error"])


def test_unprobeable_primary_cannot_publish(tmp_path, monkeypatch):
    # The builder defaults to a real health probe: without a usable token the
    # run must stop before touching the store instead of publishing blind.
    monkeypatch.delenv("TUSHARE_2K_TOKEN", raising=False)
    pipeline = build_tushare_v2_pipeline(tmp_path)
    assert pipeline.schema.version == "v2"
    assert pipeline.contract.primary_source == "tushare"
    assert pipeline.session_key == "session_date"
    report = pipeline.run(TRADE_DATE, INSTRUMENTS)
    assert report.status == "primary_source_unhealthy"
    assert not list((tmp_path / "canonical").glob("**/*")) if (tmp_path / "canonical").exists() else True


def _membership_store(root: Path, snapshot: date, codes: tuple[str, ...]) -> IndexMembershipStore:
    store = IndexMembershipStore(root)
    store.publish(
        universe_id="a500",
        index_code="000510.SH",
        source="tushare:index_weight:000510.SH",
        source_fetched_at=datetime(2025, 10, 8, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        snapshot_date=snapshot,
        publish_lag_days=2,
        trading_days=[date(2025, 10, day) for day in range(9, 20)],
        members=pd.DataFrame({"instrument": list(codes)}),
    )
    return store


class _StubPipeline:
    def __init__(self, calls: dict) -> None:
        self.calls = calls

    def run(self, trade_date, instruments):
        self.calls["date"] = trade_date
        self.calls["instruments"] = list(instruments)
        return IngestReport(
            trade_date=trade_date,
            status="published",
            rows=len(instruments),
            source_statuses={"tushare": "ok"},
            warnings=(),
            errors=(),
        )


def test_cli_v2_resolves_members_from_the_membership_store(tmp_path, monkeypatch, capsys):
    # The 500-instrument pilot could only be driven by an inline loop because the
    # documented CLI hard-wired the research-v1 whitelist universe. v2 now takes
    # its members from the PIT membership store so the pilot is reproducible.
    from uq import cli

    calls: dict = {}
    monkeypatch.setattr(cli, "build_tushare_v2_pipeline", lambda *args, **kwargs: _StubPipeline(calls))
    store = _membership_store(tmp_path / "membership", date(2025, 9, 30), ("000001.XSHE", "600000.XSHG"))

    code = cli.uq_ingest(
        [
            "daily",
            "--date",
            "2025-10-14",
            "--data-root",
            str(tmp_path / "bars"),
            "--pipeline",
            "v2",
            "--membership-root",
            str(tmp_path / "membership"),
            "--universe-id",
            "a500",
        ]
    )
    assert code == 0, capsys.readouterr().out
    assert calls["date"] == date(2025, 10, 14)
    assert calls["instruments"] == ["000001.XSHE", "600000.XSHG"]
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "published"
    assert report["rows"] == 2


def test_cli_v2_requires_governed_membership_inputs(tmp_path, monkeypatch, capsys):
    from uq import cli

    monkeypatch.setenv("UQ_SKIP_HEALTH_PROBE", "1")
    with pytest.raises(SystemExit) as exit_info:
        cli.uq_ingest(["daily", "--date", "2025-10-14", "--data-root", str(tmp_path), "--pipeline", "v2"])
    assert exit_info.value.code == 2
    usage_error = capsys.readouterr().err
    assert "--membership-root" in usage_error and "--universe-id" in usage_error


def test_cli_v2_persists_the_ingest_run_report(tmp_path, monkeypatch, capsys):
    # The pilot archive cites <bars_root>/runs/<run_id>.json as the durable
    # per-session ingest report, so the v2 path has to actually write it.
    from uq import cli

    calls: dict = {}
    monkeypatch.setattr(cli, "build_tushare_v2_pipeline", lambda *args, **kwargs: _StubPipeline(calls))
    _membership_store(tmp_path / "membership", date(2025, 9, 30), ("000001.XSHE", "600000.XSHG"))

    code = cli.uq_ingest(
        [
            "daily",
            "--date",
            "2025-10-14",
            "--data-root",
            str(tmp_path / "bars"),
            "--pipeline",
            "v2",
            "--membership-root",
            str(tmp_path / "membership"),
            "--universe-id",
            "a500",
        ]
    )
    assert code == 0, capsys.readouterr().out
    capsys.readouterr()
    reports = list((tmp_path / "bars/runs").glob("*.json"))
    assert len(reports) == 1
    recorded = json.loads(reports[0].read_text(encoding="utf-8"))
    assert recorded["status"] == "published"
    assert recorded["rows"] == 2
    assert recorded["run_id"].startswith("2025-10-14-")
    assert recorded["recorded_at"]


def test_cli_v2_fails_closed_when_membership_is_not_visible_yet(tmp_path, monkeypatch, capsys):
    from uq import cli

    calls: dict = {}
    monkeypatch.setattr(cli, "build_tushare_v2_pipeline", lambda *args, **kwargs: _StubPipeline(calls))
    _membership_store(tmp_path / "membership", date(2025, 9, 30), ("000001.XSHE",))

    code = cli.uq_ingest(
        [
            "daily",
            "--date",
            "2025-10-01",
            "--data-root",
            str(tmp_path / "bars"),
            "--pipeline",
            "v2",
            "--membership-root",
            str(tmp_path / "membership"),
            "--universe-id",
            "a500",
        ]
    )
    assert code == 3
    assert "configuration_failure" in capsys.readouterr().out
    assert calls == {}
