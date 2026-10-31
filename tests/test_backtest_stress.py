from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import pandas as pd

from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.contracts.gate_contracts import sha256_json
from uq.errors import ContractError
from uq.evaluation.stress import BacktestStressStore, run_backtest_stress
from tests.test_backtest_execution_facts import governed_inputs, published_portfolio
from tests.test_research_chain_phase4 import _config_decision, _result_decision

POLICY = {'minimum_fill_ratio': .95, 'maximum_risk_breach_sessions': 0, 'require_positive_return': False}


def baseline(root, **kwargs):
    config, prices, facts = governed_inputs(root, **kwargs)
    definition, targets, frames = published_portfolio(root, config)
    manifest, artifacts = BacktestEngine(root).run(config=config, portfolio_definition=definition,
        weight_partitions=frames, price_panel=prices, execution_facts=facts)
    BacktestResultStore(root).publish(manifest, artifacts, quality_decision=_result_decision(),
        target_weight_generations=[{'decision_date': day, 'generation_id': target['generation_id'],
            'manifest_digest_sha256': target['manifest_digest_sha256']} for day, target in targets.items()])
    return manifest


def stress(root, base, **kwargs):
    return run_backtest_stress(root, base['generation_id'], capital_grid=kwargs.pop('capital_grid', [10000., 100000., 1000000.]),
        capacity_policy=kwargs.pop('capacity_policy', POLICY),
        config_quality_decision=kwargs.pop('config_quality_decision', _config_decision()),
        result_quality_decision=kwargs.pop('result_quality_decision', _result_decision()), **kwargs)


def test_cost_sensitivity_and_capacity_replay_real_stores_and_recompute_immutable_report(tmp_path):
    base = baseline(tmp_path)
    base_path = tmp_path / 'backtest_results' / f"result={base['generation_id']}" / 'manifest.json'
    before = base_path.read_bytes()
    report = stress(tmp_path, base)
    scenarios = {row['name']: row for row in report['scenarios']}
    assert scenarios['commission_x2']['metrics']['total_commission_cny'] == 20.
    assert scenarios['slippage_x2']['metrics']['total_slippage_cny'] == pytest.approx(4.)
    assert scenarios['commission_x2']['metrics']['cumulative_return'] < base['summary_metrics']['cumulative_return']
    assert scenarios['capital_2']['metrics']['fill_ratio'] < .95
    assert report['conclusions']['maximum_tested_capital_meeting_policy'] == 100000.
    assert report['conclusions']['positive_base_and_stress_returns'] is False
    assert report['conclusions']['market_capacity_certified'] is False
    assert BacktestStressStore(tmp_path).read(report['generation_id']) == report
    assert base_path.read_bytes() == before
    # A retry reads existing reviewed configs/results and preserves identities.
    assert stress(tmp_path, base) == report


def test_result_retry_does_not_depend_on_publisher_error_wording(tmp_path, monkeypatch):
    from uq.evaluation.stress import _publish_result

    result = baseline(tmp_path)
    stored, frames = BacktestResultStore(tmp_path).read(result['generation_id'])

    def changed_wording(self, *args, **kwargs):
        raise ContractError('immutable partition collision')

    monkeypatch.setattr(BacktestResultStore, 'publish', changed_wording)
    restored = _publish_result(
        tmp_path, stored, frames, stored['target_weight_bindings'], _result_decision())
    assert restored == stored


def test_v4_stress_inputs_replay_the_bound_dividend_event_panel(tmp_path, monkeypatch):
    import uq.evaluation.stress as module
    from tests.test_backtest_v4_corporate_action_engine import DAYS, _action_panel

    config = {
        "contract_version": 4, "start_date": DAYS[0], "end_date": DAYS[1],
        "price_source_binding": {"set_digest_sha256": "a" * 64},
        "execution_facts_binding": {"generation_id": "b" * 64},
        "calendar_binding": {"generation_id": "c" * 64, "checksum_sha256": "d" * 64},
        "suspension_binding": {"dataset_generation_id": "e" * 64,
                               "data_checksum_sha256": "f" * 64},
        "corporate_action_binding": {"dataset_generation_id": "1" * 64,
                                     "data_checksum_sha256": "2" * 64},
        "universe_binding": {"snapshot_generation_id": "3" * 64},
    }
    base = {
        "portfolio_definition_generation_id": "4" * 64,
        "target_weight_bindings": [{
            "decision_date": DAYS[0], "generation_id": "5" * 64,
            "manifest_digest_sha256": "6" * 64,
        }],
    }
    events = _action_panel().loc[lambda frame: frame["event_type"].isin(
        ["rights_issue_coverage", "dividend_coverage"]
    )].reset_index(drop=True)

    class PriceStore:
        def __init__(self, _root):
            pass

        def read_window(self, *_args):
            return config["price_source_binding"], pd.DataFrame([
                {"session_date": day, "instrument": "600000.XSHG", "open": 10.,
                 "close": 10., "volume": 1000.}
                for day in DAYS[:2]
            ])

    class FactStore:
        def __init__(self, _root):
            pass

        def read(self, _generation):
            return {"manifest": "synthetic"}, pd.DataFrame()

    class MarketStore:
        def __init__(self, _root):
            pass

        def read_frame(self, _generation, _checksum, *, dataset_type, required_columns):
            if dataset_type == "calendar":
                return pd.DataFrame({"date": DAYS[:2]})
            if dataset_type == "suspension":
                return pd.DataFrame(columns=["date", "instrument", "suspended"])
            assert dataset_type == "corporate_action_events"
            assert set(required_columns) <= set(events.columns)
            return events

    class WeightStore:
        def __init__(self, _root):
            pass

        def read(self, _generation, _day):
            return {
                "manifest_digest_sha256": "6" * 64,
                "portfolio_definition_generation_id": base["portfolio_definition_generation_id"],
                "universe_snapshot_generation_id": config["universe_binding"]["snapshot_generation_id"],
                "decision_date": DAYS[0],
            }, pd.DataFrame({"instrument": ["600000.XSHG"], "weight": [0.1]})

    monkeypatch.setattr(module, "RawExecutionPriceStore", PriceStore)
    monkeypatch.setattr(module, "ExecutionFactsStore", FactStore)
    monkeypatch.setattr(module, "BacktestMarketDatasetStore", MarketStore)
    monkeypatch.setattr(module, "TargetWeightStore", WeightStore)
    monkeypatch.setattr(module, "facts_binding", lambda _manifest: config["execution_facts_binding"])
    monkeypatch.setattr(module, "reject_symlinks_within", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_verify_review", lambda *_args, **_kwargs: None)

    replay = module._inputs(tmp_path, base, config)
    pd.testing.assert_frame_equal(replay["corporate_action_events"], events)
    assert "corporate_action_instruments" not in replay


def test_half_fill_stress_actually_changes_fills_and_is_not_assumed_to_reduce_profit(tmp_path):
    base = baseline(tmp_path, capital=100000.)
    report = stress(tmp_path, base, capital_grid=[100000.])
    reduced = next(row for row in report['scenarios'] if row['name'] == 'fill_rate_half')
    result, frames = BacktestResultStore(tmp_path).read(reduced['result_binding']['generation_id'])
    assert frames['fills']['filled_shares'].tolist() == [500, 500]
    assert reduced['metrics']['fill_ratio'] == pytest.approx(2 / 3)
    assert result['backtest_config_generation_id'] == reduced['config_binding']['generation_id']


def test_positive_capacity_policy_does_not_qualify_a_losing_strategy(tmp_path):
    base = baseline(tmp_path)
    report = stress(tmp_path, base, capital_grid=[10000.], capacity_policy={**POLICY, 'require_positive_return': True})
    assert report['conclusions']['maximum_tested_capital_meeting_policy'] is None


@pytest.mark.parametrize('grid', [[], [float('nan')], [0.], [10000., 10000.], [True]])
def test_invalid_capacity_grid_is_refused_before_publishing_variants(tmp_path, grid):
    base = baseline(tmp_path)
    before = sorted((tmp_path / 'backtest_configs').glob('generation=*'))
    with pytest.raises(ContractError, match='capital grid'):
        stress(tmp_path, base, capital_grid=grid)
    assert sorted((tmp_path / 'backtest_configs').glob('generation=*')) == before


def test_rehashed_cost_report_cannot_change_derived_capacity_conclusion(tmp_path):
    base = baseline(tmp_path)
    report = stress(tmp_path, base, capital_grid=[10000.])
    changed = copy.deepcopy(report)
    changed['conclusions']['maximum_tested_capital_meeting_policy'] = 90000000.
    payload = {key: value for key, value in changed.items() if key not in {'generation_id', 'manifest_digest_sha256'}}
    changed['generation_id'] = sha256_json(payload)
    changed['manifest_digest_sha256'] = sha256_json({**payload, 'generation_id': changed['generation_id']})
    path = tmp_path / 'backtest_stress' / f"generation={changed['generation_id']}" / 'manifest.json'
    path.parent.mkdir()
    path.write_text(json.dumps(changed))
    with pytest.raises(ContractError, match='do not recompute'):
        BacktestStressStore(tmp_path).read(changed['generation_id'])


def test_baseline_must_reproduce_before_cost_variants_are_published(tmp_path, monkeypatch):
    base = baseline(tmp_path)
    original = BacktestEngine.run
    def changed_engine(self, **kwargs):
        manifest, frames = original(self, **kwargs)
        frames['equity_curve'].loc[0, 'portfolio_value'] += 1
        return manifest, frames
    monkeypatch.setattr(BacktestEngine, 'run', changed_engine)
    before = sorted((tmp_path / 'backtest_configs').glob('generation=*'))
    with pytest.raises(ContractError, match='baseline replay differs'):
        stress(tmp_path, base)
    assert sorted((tmp_path / 'backtest_configs').glob('generation=*')) == before


def test_stress_report_reverifies_raw_source_bytes(tmp_path):
    base = baseline(tmp_path)
    report = stress(tmp_path, base, capital_grid=[10000.])
    path = next((tmp_path / 'canonical/bars_daily/v2').glob('date=*/data.parquet'))
    with path.open('ab') as stream:
        stream.write(b'tamper')
    with pytest.raises(ContractError, match='checksum'):
        BacktestStressStore(tmp_path).read(report['generation_id'])


def test_rehashed_report_cannot_substitute_baseline_for_a_reviewed_cost_variant(tmp_path):
    base = baseline(tmp_path)
    report = stress(tmp_path, base, capital_grid=[10000.])
    report['scenarios'][0]['result_binding'] = {name: base[name] for name in
                                               ('generation_id', 'manifest_digest_sha256')}
    payload = {key: value for key, value in report.items() if key not in {'generation_id', 'manifest_digest_sha256'}}
    report['generation_id'] = sha256_json(payload)
    report['manifest_digest_sha256'] = sha256_json({**payload, 'generation_id': report['generation_id']})
    path = tmp_path / 'backtest_stress' / f"generation={report['generation_id']}" / 'manifest.json'
    path.parent.mkdir()
    path.write_text(json.dumps(report))
    with pytest.raises(ContractError, match='outside its declared variant'):
        BacktestStressStore(tmp_path).read(report['generation_id'])


def test_old_review_cannot_authorize_new_cost_economics(tmp_path):
    base = baseline(tmp_path)
    wrong_subject = _config_decision()('f' * 64)
    before = sorted((tmp_path / 'backtest_configs').glob('generation=*'))
    with pytest.raises(ContractError, match='subject|bound|generation'):
        stress(tmp_path, base, config_quality_decision=wrong_subject)
    assert sorted((tmp_path / 'backtest_configs').glob('generation=*')) == before
    assert not (tmp_path / 'backtest_stress').exists()


def test_cost_batch_cli_uses_external_reviewer_for_both_new_configs_and_results(tmp_path):
    from tests.test_publish_backtest_config import _decisions_document, _reviewer_script

    base = baseline(tmp_path)
    decisions = tmp_path / 'decisions.json'
    _decisions_document(decisions, _reviewer_script(tmp_path / 'reviewer.py'))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/run_backtest_stress.py'),
        '--data-root', str(tmp_path), '--baseline-generation', base['generation_id'],
        '--capital-grid', '10000', '--minimum-fill-ratio', '.95', '--maximum-risk-breach-sessions', '0',
        '--no-require-positive-return', '--quality-decisions-json', str(decisions)],
        capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert len(report['scenarios']) == 4
    assert BacktestStressStore(tmp_path).read(report['generation_id']) == report
