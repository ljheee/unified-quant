"""`backtest_config.v2` binds the window's adjusted-price partition set.

The per-session adjusted line's invariants (`adj_factor` positive, restatement
equals the vendor value, jump agreement with the prior partition) are all *between*
adjacent partitions, so the line stays per-session and the window frame is
assembled at read time.  These tests drive the real store, the real publisher and
the real backtest stage, and lock the three invariants that make an assembled frame
equivalent to a stored one.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import UniverseSnapshotStore
from uq.errors import ContractError
from uq.execution.set_binding_resolver import set_digest_sha256, verify_partition_set_binding
from uq.research_chain.owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    BacktestMarketDatasetStore,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SCRIPT = ROOT / "scripts" / "publish_backtest_config.py"
MARKET_SCRIPT = ROOT / "scripts" / "publish_backtest_market_data.py"
SESSIONS = [dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]
START, END = SESSIONS[0], SESSIONS[-1]
FAMILY = "bars_adjusted_adjusted_v2"


def _publish_adjusted(root: Path, sessions: list[dt.date]) -> list[str]:
    """Publish one canonical v2 adjusted partition per session; return generations."""
    from uq.contracts.artifacts import QualityReportStore
    from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
    from uq.contracts.schema import load_schema

    schema = load_schema("config/schemas/bars_adjusted.adjusted-v2.yaml")
    generations: list[str] = []
    for session in sessions:
        rows = [
            {
                "instrument": instrument, "session_date": pd.Timestamp(session),
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                "volume": 1000.0, "amount": 10000.0, "status": "trading",
                "limit_up": 11.0, "limit_down": 9.0, "adj_factor": 1.0,
            }
            for instrument in ("600000.XSHG", "600519.XSHG")
        ]
        frame = pd.DataFrame(rows)
        store = CanonicalV2Store(root)
        generation = store.prepare_generation(schema, session, frame, {}, {})
        report = {
            "report_version": 1, "binding_type": "canonical_v2",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                        "level": "error", "result": "passed"}],
            "errors": [], "warnings": [],
        }
        directory = QualityReportStore().save(root, report)
        checksum = file_sha256_bytes((directory / "report.json").read_bytes())
        store.publish(schema, session, frame, {}, {}, quality_checksum=checksum)
        generations.append(generation)
    return generations


def _run(data_root: Path, dividend_root: Path, universe_generation: str, **overrides):
    arguments = [
        sys.executable, str(CONFIG_SCRIPT),
        "--data-root", str(data_root), "--universe-root", str(data_root),
        "--universe-id", "csi-all", "--universe-snapshot", universe_generation,
        "--market-inputs", str(data_root / "market-inputs.json"),
        "--start", START.isoformat(), "--end", END.isoformat(),
        "--backtest-name", "research-baseline",
        "--quality-decisions-json", str(data_root / "decisions.json"),
    ]
    for key, value in overrides.items():
        if value is not None:
            arguments += [f"--{key.replace('_', '-')}", str(value)]
    return arguments


def _universe_snapshot(root: Path) -> str:
    
    store = UniverseSnapshotStore()
    store.save(
        root,
        {
            "universe_version": 1, "universe_id": "csi-all",
            "source": "tushare:index_weight:000985.CSI",
            "membership_evidence": "source=tushare:index_weight:000985.CSI pit_precision=monthly_approximation",
            "snapshot_time": "2026-01-01T00:00:00+08:00",
            "valid_from": START.isoformat(), "valid_to": END.isoformat(),
            "visibility_time": START.isoformat() + "T00:00:00+08:00",
            "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
        },
        pd.DataFrame({"instrument": ["600000.XSHG", "600519.XSHG"]}),
    )
    published = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (root / "universes" / "csi-all").glob("*/manifest.json")
    ][0]
    return published["generation_id"]


def _reviewer(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, sys\n"
        "sys.path.insert(0, {root!r})\n"
        "from tests.review_key import REVIEWER_PRIVATE_KEY\n"
        "from uq.contracts.model_layer import (\n"
        "    _MODEL_QUALITY_REVIEW_REGISTRY, create_reviewed_quality_decision)\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--binding-type', required=True)\n"
        "parser.add_argument('--subject-generation-id', required=True)\n"
        "parser.add_argument('--subject-manifest-digest-sha256', default='')\n"
        "parser.add_argument('--output-family', default='')\n"
        "parser.add_argument('--provider-config-ref', default='')\n"
        "args = parser.parse_args()\n"
        "registry = _MODEL_QUALITY_REVIEW_REGISTRY.bindings[args.binding_type]\n"
        "print(json.dumps(create_reviewed_quality_decision(\n"
        "    binding_type=args.binding_type, policy=registry['policy'], status='passed',\n"
        "    checks=[{{'name': n, 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}}\n"
        "            for n in registry['allowed_checks']],\n"
        "    errors=[], warnings=[], producer_code_fingerprint='0' * 64,\n"
        "    private_key_pem=REVIEWER_PRIVATE_KEY,\n"
        "    subject_generation_id=args.subject_generation_id,\n"
        "    subject_content_sha256=args.subject_generation_id)))\n"
        .format(root=str(ROOT)),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _prepare(tmp_path: Path) -> tuple[str, dict]:
    """Publish adjusted partitions, market inputs, universe, reviewer, decisions."""
    from tests.test_publish_backtest_market_data import _canonical_bars, _dividend_store

    _publish_adjusted(tmp_path, SESSIONS)
    # market inputs need the canonical bars line, which is a different tree
    _canonical_bars(tmp_path, SESSIONS)
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    market = subprocess.run(
        [sys.executable, str(MARKET_SCRIPT), "--data-root", str(tmp_path),
         "--dividend-root", str(tmp_path / "dividend-store"),
         "--start", START.isoformat(), "--end", END.isoformat()],
        capture_output=True, text=True, check=True,
    )
    (tmp_path / "market-inputs.json").write_text(market.stdout, encoding="utf-8")
    (tmp_path / "decisions.json").write_text(json.dumps({
        "provider_id": "external-model-quality-reviewer-v1",
        "provider_command": [sys.executable, str(_reviewer(tmp_path / "reviewer.py"))],
    }), encoding="utf-8")
    return _universe_snapshot(tmp_path), json.loads(market.stdout)


def _execute(root: Path, universe: str, **overrides) -> dict:
    result = subprocess.run(
        _run(root, root / "dividend-store", universe, **overrides),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return json.loads(result.stdout)


def test_a_v2_config_binds_the_whole_window_as_a_partition_set(tmp_path: Path) -> None:
    universe, _market = _prepare(tmp_path)
    report = _execute(tmp_path, universe)
    manifest = BacktestConfigStore(tmp_path).read_manifest(report["generation_id"])
    assert manifest["contract_version"] == 2
    binding = manifest["price_source_binding"]
    assert set(binding) == {"family", "window_start", "window_end",
                            "set_digest_sha256", "member_count"}
    assert binding["family"] == FAMILY
    assert binding["window_start"] == START.isoformat()
    assert binding["window_end"] == END.isoformat()
    assert binding["member_count"] == len(SESSIONS)

    # the bound set digest is recomputable from the store, and it matches
    store = AdjustedPriceDatasetStore(tmp_path)
    triples = store.list_partitions_in_window(START, END)
    assert set_digest_sha256(triples) == binding["set_digest_sha256"]
    verify_partition_set_binding(binding, triples, family=FAMILY)


def test_the_backtest_stage_assembles_the_window_frame_at_read_time(tmp_path: Path) -> None:
    from tests.test_research_chain_phase4 import (
        _config_decision, _prepare_portfolio, _result_decision,
    )

    from uq.backtest.engine import BacktestEngine, BacktestResultStore
    from uq.research_chain import BacktestStageAdapter

    plan, portfolio, portfolio_result, _ = _prepare_portfolio(tmp_path)
    universe = portfolio_result.definition_manifest["universe_snapshot_generation_id"]

    # v2's own inputs live in a second root, because the market-input families
    # (calendar/suspension/corporate-action) are window-level artifacts and a
    # second publication for the same window would collide with phase4's.
    v2_root = tmp_path / "v2-inputs"
    v2_root.mkdir()
    _prepare(v2_root)
    report = _execute(v2_root, universe)
    config = BacktestConfigStore(v2_root).read_manifest(report["generation_id"])
    assert config["universe_binding"]["snapshot_generation_id"] == universe

    # Rebind the plan to v2's stores and config, then run the real stage.
    adapter = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(v2_root), run_store=portfolio.run_store,
        adjusted_price_store=AdjustedPriceDatasetStore(v2_root),
        market_dataset_store=BacktestMarketDatasetStore(v2_root),
    )
    # the price panel's adjusted_price_dataset binding is v1-shaped, and the v2
    # path ignores it, so only the config binding needs rebinding here
    plan = _plan_with_config(plan, config)
    result = adapter.run(
        plan, portfolio_stage_result=portfolio_result,
        quality_decision=_result_decision(), config_quality_decision=_config_decision(),
        runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test",
                         "lock_digest_sha256": "0" * 64},
    )
    assert result.result_manifest["contract_version"] == 2
    assert set(result.result_artifacts) == {"equity_curve", "daily_metrics", "fills"}
    assert result.result_manifest["price_source_binding"]["family"] == FAMILY
    assert result.result_manifest["price_source_binding"]["member_count"] == len(SESSIONS)

    # and the window frame is three sessions x two instruments, keys unique
    store = AdjustedPriceDatasetStore(v2_root)
    frame = store.read_partition_frames(store.list_partitions_in_window(START, END))
    assert len(frame) == len(SESSIONS) * 2
    assert not frame.duplicated(subset=["instrument", "session_date"]).any()


def test_a_v2_config_review_is_reverified_under_the_v2_contract(tmp_path: Path) -> None:
    """The config review's subject must be derived under the config's own schema.

    A v2 config is a different contract from v1 (partition-set price binding), so
    its provisional review subject is a different content hash.  Deriving that
    subject with the v1 schema made the stage reject a config whose review was
    valid -- and the test suite had only exercised the publisher, not the stage
    readback, with a v2 config.
    """
    from tests.test_research_chain_phase4 import (
        _config_decision, _prepare_portfolio, _result_decision,
    )

    from uq.backtest.engine import BacktestEngine, BacktestResultStore
    from uq.research_chain import BacktestStageAdapter

    plan, portfolio, portfolio_result, _ = _prepare_portfolio(tmp_path)
    universe = portfolio_result.definition_manifest["universe_snapshot_generation_id"]
    v2_root = tmp_path / "v2-inputs"
    v2_root.mkdir()
    _prepare(v2_root)
    report = _execute(v2_root, universe)
    config = BacktestConfigStore(v2_root).read_manifest(report["generation_id"])
    assert config["contract_version"] == 2

    adapter = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(v2_root), run_store=portfolio.run_store,
        adjusted_price_store=AdjustedPriceDatasetStore(v2_root),
        market_dataset_store=BacktestMarketDatasetStore(v2_root),
    )
    # The stage's own v2 review check is the behaviour under test; reaching it
    # without a schema-mismatch failure is the assertion.
    result = adapter.run(
        _plan_with_config(plan, config),
        portfolio_stage_result=portfolio_result,
        quality_decision=_result_decision(),
        config_quality_decision=_config_decision(),
        runner_identity={
            "code_fingerprint": "0" * 64,
            "environment_profile": "locked-test",
            "lock_digest_sha256": "0" * 64,
        },
    )
    assert result.result_manifest["contract_version"] == 2


def _plan_with_config(plan, config):
    import dataclasses
    from uq.research_chain.resolver import ResolvedStageBinding

    return dataclasses.replace(
        plan,
        stage_bindings=tuple(
            ResolvedStageBinding(
                stage=b.stage, output_family=b.output_family, generation_id=b.generation_id,
                manifest_digest_sha256=b.manifest_digest_sha256,
                data_checksum_sha256=b.data_checksum_sha256,
            ) if b.output_family != "backtest_config" else ResolvedStageBinding(
                stage=b.stage, output_family=b.output_family,
                generation_id=config["generation_id"],
                manifest_digest_sha256=config["manifest_digest_sha256"],
                data_checksum_sha256=b.data_checksum_sha256,
            )
            for b in plan.stage_bindings
        ),
    )


def test_a_tampered_member_is_refused_rather_than_concatenated(tmp_path: Path) -> None:
    universe, _market = _prepare(tmp_path)
    report = _execute(tmp_path, universe)
    config = BacktestConfigStore(tmp_path).read_manifest(report["generation_id"])
    store = AdjustedPriceDatasetStore(tmp_path)
    triple = store.list_partitions()[0]
    data_path = (
        tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2"
        / f"date={triple['session_or_decision_date']}" / "data.parquet"
    )
    data_path.write_bytes(b"not parquet")
    with pytest.raises(ContractError):
        store.read_partition_frames(store.list_partitions())


def test_a_duplicate_key_across_partitions_is_refused(tmp_path: Path) -> None:
    _prepare(tmp_path)
    # Republish the first partition's rows under the second partition's identity
    # is impossible (identity is a content hash), so construct the duplicate the
    # way the join would see it: two manifests naming the same rows.
    store = AdjustedPriceDatasetStore(tmp_path)
    triples = store.list_partitions()
    assert triples
    with pytest.raises(ContractError, match="unique|duplicate"):
        store.read_partition_frames([triples[0], triples[0]])


def test_a_window_reaching_past_the_published_partitions_yields_a_smaller_set(tmp_path: Path) -> None:
    """The set is the intersection of the window and what is published.

    A window that reaches further than the published partitions produces a
    *shorter* set, and `verify_partition_set_binding` then behaves exactly as it
    does for factor and paper bindings: the member count and each member's date
    are checked against the declared window.  So the invariant that matters is
    that a bound set is always self-consistent, never that the window is refused
    because data is missing -- a missing session is a readiness-gate question.
    """
    universe, _market = _prepare(tmp_path)
    wider = dt.date(2030, 1, 1)
    report = _execute(tmp_path, universe, end=wider.isoformat())
    manifest = BacktestConfigStore(tmp_path).read_manifest(report["generation_id"])
    binding = manifest["price_source_binding"]
    # member_count still matches the partitions that exist and are in the window
    triples = AdjustedPriceDatasetStore(tmp_path).list_partitions_in_window(START, wider)
    assert binding["member_count"] == len(triples) == len(SESSIONS)
    verify_partition_set_binding(binding, triples, family=FAMILY)
    assert set_digest_sha256(triples) == binding["set_digest_sha256"]
    # a member outside the declared window is refused by name (member_count is
    # checked first, so the same count is carried into the mutated set)
    with pytest.raises(ContractError, match="outside binding window"):
        verify_partition_set_binding(
            {**binding, "member_count": len(triples) + 1},
            [*triples, dict(triples[0], session_or_decision_date="1999-01-01")],
            family=FAMILY,
        )


def test_a_published_history_outside_the_bound_window_is_not_in_the_set(tmp_path: Path) -> None:
    """A store's history must not widen the set a config binds.

    The real adjusted store keeps a jump-baseline session (and any earlier
    publications) beside the sessions a backtest trades.  Enumerating every
    partition made a config built for ``[START, END]`` fail its own member-count
    check as soon as the store held one extra session.  The window filter is the
    definition shared by the publisher and the backtest readback.
    """
    universe, _market = _prepare(tmp_path)
    report = _execute(tmp_path, universe)
    config = BacktestConfigStore(tmp_path).read_manifest(report["generation_id"])
    binding = config["price_source_binding"]

    # Publish one more adjusted session before the bound window.
    earlier = START - dt.timedelta(days=1)
    _publish_adjusted(tmp_path, [earlier])
    store = AdjustedPriceDatasetStore(tmp_path)
    assert len(store.list_partitions()) == len(SESSIONS) + 1

    triples = store.list_partitions_in_window(START, END)
    assert [item["session_or_decision_date"] for item in triples] == [
        session.isoformat() for session in SESSIONS
    ]
    verify_partition_set_binding(binding, triples, family=FAMILY)
    assert set_digest_sha256(triples) == binding["set_digest_sha256"]


def test_the_backtest_refuses_a_price_binding_whose_digest_is_wrong(tmp_path: Path) -> None:
    """The set binding is verified, not merely enumerated.

    Without this check the stage would read whatever partitions exist and ignore
    the digest the config committed to -- so a config naming one set of partitions
    could be executed against another while the result still claimed the first.
    The mutation (skip the verification) survives every other test in this module,
    which is why this lock exists.
    """
    from tests.test_research_chain_phase4 import (
        _config_decision, _prepare_portfolio, _result_decision,
    )

    from uq.backtest.engine import BacktestEngine, BacktestResultStore
    from uq.contracts.model_layer import model_manifest_identities
    from uq.research_chain import BacktestStageAdapter
    from uq.research_chain.owning_contracts import BacktestConfigStore

    plan, portfolio, portfolio_result, _ = _prepare_portfolio(tmp_path)
    universe = portfolio_result.definition_manifest["universe_snapshot_generation_id"]
    v2_root = tmp_path / "v2-inputs"
    v2_root.mkdir()
    _prepare(v2_root)
    report = _execute(v2_root, universe)
    config = BacktestConfigStore(v2_root).read_manifest(report["generation_id"])

    # The adapter reads the config from its store, so the tampered binding has to be
    # published -- a store re-read of the honest manifest would prove nothing.
    def _republish(price_binding: dict) -> dict:
        payload = {
            key: value for key, value in config.items()
            if key not in {"generation_id", "manifest_digest_sha256",
                           "quality_report_checksum_sha256"}
        }
        payload["price_source_binding"] = price_binding
        provisional, _ = model_manifest_identities(
            {**payload, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="backtest_config_v2",
        )
        from uq.contracts.model_layer import (
            bind_reviewed_quality_decision, create_reviewed_quality_decision,
            _MODEL_QUALITY_REVIEW_REGISTRY,
        )
        from tests.review_key import REVIEWER_PRIVATE_KEY

        registry = _MODEL_QUALITY_REVIEW_REGISTRY.bindings["backtest_config_v1"]
        decision = create_reviewed_quality_decision(
            binding_type="backtest_config_v1", policy=registry["policy"], status="passed",
            checks=[{"name": n, "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
                    for n in registry["allowed_checks"]],
            errors=[], warnings=[], producer_code_fingerprint="0" * 64,
            private_key_pem=REVIEWER_PRIVATE_KEY,
            subject_generation_id=provisional, subject_content_sha256=provisional,
        )
        bound, checksum = bind_reviewed_quality_decision(
            decision, binding_type="backtest_config_v1", subject_generation_id=provisional,
        )
        payload["quality_report_checksum_sha256"] = checksum
        return BacktestConfigStore(v2_root).publish(payload, quality_decision=decision)

    adapter = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(v2_root), run_store=portfolio.run_store,
        adjusted_price_store=AdjustedPriceDatasetStore(v2_root),
        market_dataset_store=BacktestMarketDatasetStore(v2_root),
    )
    runner_identity = {"code_fingerprint": "0" * 64, "environment_profile": "locked-test",
                       "lock_digest_sha256": "0" * 64}

    wrong_digest = _republish({**config["price_source_binding"], "set_digest_sha256": "9" * 64})
    with pytest.raises(ContractError, match="partition set digest mismatch"):
        adapter.run(
            _plan_with_config(plan, wrong_digest), portfolio_stage_result=portfolio_result,
            quality_decision=_result_decision(), config_quality_decision=_config_decision(),
            runner_identity=runner_identity,
        )

    wrong_count = _republish({**config["price_source_binding"], "member_count": 99})
    with pytest.raises(ContractError, match="partition set member count mismatch"):
        adapter.run(
            _plan_with_config(plan, wrong_count), portfolio_stage_result=portfolio_result,
            quality_decision=_result_decision(), config_quality_decision=_config_decision(),
            runner_identity=runner_identity,
        )
