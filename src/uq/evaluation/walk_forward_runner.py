"""Execute pre-reviewed fold requests without changing owning-layer contracts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..contracts.gate_contracts import canonical_json
from ..contracts.model_layer import ModelContractLoader
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..models.dataset_writer import DatasetWriter
from ..research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore, LabelStore
from ..research_chain.provenance import capture_environment, verified_runner_identity
from ..research_chain.resolver import FileResearchRunStore
from .report import _verified_result, _verify_review, published_ic_inputs
from .walk_forward import (
    WalkForwardSpec, WalkForwardStore, plan_from_published_calendar,
    validate_fold_requests, verify_walk_forward_plan,
)


@dataclass(frozen=True)
class ResearchFoldJob:
    request: dict
    data_root: Path
    decisions_document: dict
    provider_document: dict | None = None
    membership_root: Path | None = None
    market_dataset_root: Path | None = None
    paper_risk_documents_path: Path | None = None


class FoldExecutor(Protocol):
    def preflight(self, plan: dict, jobs: list[ResearchFoldJob]) -> None: ...
    def execute(self, job: ResearchFoldJob) -> dict: ...
    def verify(self, fold: dict, job: ResearchFoldJob, outcome: dict) -> dict: ...


class ResearchFoldExecutor:
    """Delegate fitting/publication/resume to the accepted single-request runner.

    No scores file is accepted: the owning runner infers from the artifact it
    trained. No quality decision is created here. Each request and upstream
    artifact must have been prepared and externally reviewed before execution.
    """
    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root)

    def preflight(self, plan: dict, jobs: list[ResearchFoldJob]) -> None:
        if self.project_root.resolve() != Path(__file__).resolve().parents[3]:
            raise ContractError('walk-forward project root is not the executing checkout')
        binding = plan['calendar_binding']
        if binding.get('family') != 'backtest_market_dataset_calendar' or not binding.get('store_root'):
            raise ContractError('execution requires a published-calendar plan with its store locator')
        loaded = plan_from_published_calendar(
            Path(binding['store_root']), binding['generation_id'], binding['data_checksum_sha256'],
            WalkForwardSpec(**plan['spec']),
        )
        if canonical_json(loaded) != canonical_json(plan):
            raise ContractError('walk-forward source calendar no longer matches the plan')
        environment = capture_environment(self.project_root)
        baseline = None
        for fold, job in zip(plan['folds'], jobs):
            if job.provider_document is None:
                if not isinstance(job.decisions_document.get('quality_decisions'), dict):
                    raise ContractError('walk-forward static review document requires quality_decisions')
                if not isinstance(job.decisions_document.get('request_quality_decisions', {}), dict):
                    raise ContractError('walk-forward request_quality_decisions must be an object')
            else:
                command = job.provider_document.get('provider_command')
                if (job.provider_document.get('provider_id') != 'external-model-quality-reviewer-v1'
                        or not isinstance(command, list) or not command
                        or not all(isinstance(item, str) and item for item in command)):
                    raise ContractError('walk-forward external reviewer configuration is invalid')
            if (job.membership_root is None) != (job.market_dataset_root is None):
                raise ContractError('walk-forward membership and market-dataset roots must be paired')
            if job.request['contract_version'] in {5, 6, 7, 8}:
                if job.membership_root is None or job.paper_risk_documents_path is None:
                    raise ContractError('walk-forward v5-v8 requires membership, market and paper risk documents')
                import json
                documents = json.loads(job.paper_risk_documents_path.read_text(encoding='utf-8'))
                if not isinstance(documents, dict) or not {'policy', 'policy_review'} <= set(documents):
                    raise ContractError('walk-forward paper risk documents are incomplete')
            verified_runner_identity(job.request['environment'], environment)
            label = LabelStore(job.data_root).read_manifest(job.request['label_binding']['generation_id'])
            if (label['horizon_trading_days'] != plan['spec']['label_horizon_sessions']
                    or label['manifest_digest_sha256'] != job.request['label_binding']['manifest_digest_sha256']):
                raise ContractError('walk-forward label horizon/digest disagrees with protocol/request')
            config = BacktestConfigStore(job.data_root).read_manifest(
                job.request['backtest_config_binding']['generation_id'])
            version = config['contract_version']
            family = f'backtest_config_v{version}' if version in {2, 3, 4} else 'backtest_config'
            _verify_review(job.data_root, config, family, 'backtest_config_v1')
            if config['manifest_digest_sha256'] != job.request['backtest_config_binding']['manifest_digest_sha256']:
                raise ContractError('walk-forward backtest config digest mismatch')
            if (config['start_date'], config['end_date']) != (fold['train_start'], fold['label_observation_end']):
                raise ContractError('walk-forward backtest window does not match fold')
            calendar = BacktestMarketDatasetStore(job.data_root).read_frame(
                config['calendar_binding']['generation_id'], config['calendar_binding']['checksum_sha256'],
                dataset_type='calendar', required_columns={'date'},
            )
            from .metrics import session_dates
            expected_dates = [day for day in plan['calendar_dates']
                              if fold['train_start'] <= day <= fold['label_observation_end']]
            actual_dates = session_dates(calendar['date'].tolist(), label='fold backtest calendar')
            if sorted(actual_dates) != expected_dates:
                raise ContractError('walk-forward fold calendar is incomplete or differs from protocol')
            if version == 3:
                from ..backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding

                facts, fact_rows = ExecutionFactsStore(job.data_root).read(
                    config['execution_facts_binding']['generation_id'])
                if facts_binding(facts) != config['execution_facts_binding']:
                    raise ContractError('walk-forward execution facts binding mismatch')
                _, raw = RawExecutionPriceStore(job.data_root).read_window(
                    config['start_date'], config['end_date'], config['price_source_binding'])
                import pandas as pd

                raw_dates = sorted(pd.to_datetime(raw['session_date']).dt.strftime('%Y-%m-%d').unique())
                if raw_dates != expected_dates or sorted(fact_rows['date'].unique()) != expected_dates:
                    raise ContractError('walk-forward raw prices/execution facts differ from fold calendar')
            economics = {name: config.get(name) for name in
                         ('contract_version', 'schema_version', 'cost_model', 'execution_model',
                          'limit_rules', 'initial_capital', 'risk_constraints')}
            if baseline is not None and canonical_json(economics) != canonical_json(baseline):
                raise ContractError('walk-forward cost/execution/capital settings drift across folds')
            baseline = economics

    def execute(self, job: ResearchFoldJob) -> dict:
        # A complete result is immutable. It is discovered before invoking the
        # runner, not by catching a writer's refusal or modifying a stage ledger.
        directory = (job.data_root / 'research_runs/results'
                     / f"request={job.request['request_content_generation_id']}"
                     / f"run={job.request['run_id']}")
        reject_symlinks_within(job.data_root, directory, label='walk-forward completed result')
        candidates = list(directory.glob('result=*/manifest.json'))
        if candidates:
            if len(candidates) != 1:
                raise ContractError('ambiguous completed walk-forward fold results')
            path = candidates[0]
            reject_symlinks_within(job.data_root, path, label='walk-forward completed result')
            import json
            try:
                manifest = json.loads(path.read_text(encoding='utf-8'))
                generation = path.parent.name.removeprefix('result=')
                result = FileResearchRunStore(job.data_root).read_result(generation, manifest['manifest_digest_sha256'])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, ContractError):
                    raise
                raise ContractError('completed walk-forward result is malformed') from exc
            return {'status': 'published', 'result_content_generation_id': generation,
                    'manifest_digest_sha256': result['manifest_digest_sha256'],
                    'execution_disposition': 'read_completed_result'}
        from ..research_chain.cli import _execute
        return _execute(
            job.request, decisions_document=job.decisions_document, scores_document=None,
            provider_document=job.provider_document, project_root=self.project_root,
            data_root=job.data_root, membership_root=job.membership_root,
            market_dataset_root=job.market_dataset_root,
            paper_risk_documents_path=job.paper_risk_documents_path,
        )

    def verify(self, fold: dict, job: ResearchFoldJob, outcome: dict) -> dict:
        if outcome.get('status') != 'published':
            raise ContractError('walk-forward executor did not publish a research result')
        result = FileResearchRunStore(job.data_root).read_result(
            outcome['result_content_generation_id'], outcome['manifest_digest_sha256'])
        if result['final_status'] != 'passed' or (
            result['request_content_generation_id'], result['run_id']
        ) != (job.request['request_content_generation_id'], job.request['run_id']):
            raise ContractError('walk-forward result is not the passed result of the requested fold')
        FileResearchRunStore(job.data_root).read_request(
            job.request['request_content_generation_id'], job.request['manifest_digest_sha256'],
            job.request['run_id'])
        expected_runner = verified_runner_identity(job.request['environment'], capture_environment(self.project_root))
        if result['runner_identity'] != expected_runner:
            raise ContractError('walk-forward result runner identity mismatch')
        bindings: dict[str, list[dict]] = {}
        for stage in result['stage_records']:
            for binding in stage['output_bindings']:
                bindings.setdefault(binding['output_family'], []).append(binding)

        def one(family: str) -> dict:
            if len(bindings.get(family, [])) != 1:
                raise ContractError(f'walk-forward needs one {family} output')
            return bindings[family][0]

        dataset_binding = one('model_dataset')
        dataset_path = job.data_root / dataset_binding['physical_path']
        reject_symlinks_within(job.data_root, dataset_path, label='walk-forward dataset')
        dataset = ModelContractLoader(job.data_root).load('model_dataset', dataset_path)
        if (dataset['generation_id'], dataset['manifest_digest_sha256']) != (
            dataset_binding['generation_id'], dataset_binding['manifest_digest_sha256']
        ):
            raise ContractError('walk-forward dataset generation mismatch')
        dataset, _frame = DatasetWriter(job.data_root).read(
            dataset['dataset_name'], dataset['semantic_version'], dataset['generation_id'])
        del _frame
        if dataset['split_policy'] != fold['split_policy']:
            raise ContractError('walk-forward actual dataset splits differ from protocol')
        if dataset['label_generation_id'] != job.request['label_binding']['generation_id']:
            raise ContractError('walk-forward actual dataset label binding mismatch')
        predictions = []
        for binding in bindings.get('prediction_set', []):
            day = Path(binding['physical_path']).name.removeprefix('date=')
            predictions.append({'decision_date': day, 'generation_id': binding['generation_id']})
        dates = sorted(item['decision_date'] for item in predictions)
        if dates != fold['test_dates']:
            raise ContractError('walk-forward predictions must cover every test session exactly once')
        _scores, _labels, ic_inputs = published_ic_inputs(
            job.data_root, predictions, job.data_root, job.request['label_binding']['generation_id'],
            calendar_dates=fold['test_dates'])
        if set(ic_inputs['prediction_datasets']) != {dataset['generation_id']}:
            raise ContractError('walk-forward predictions are not bound to this fold dataset')
        run_binding, artifact_binding = one('model_run'), one('model_artifact')
        run = ModelContractLoader(job.data_root).load('model_run', job.data_root / run_binding['physical_path'])
        if (run['run_content_generation_id'] != run_binding['generation_id']
                or run['manifest_digest_sha256'] != run_binding['manifest_digest_sha256']
                or run['model_dataset_generation_id'] != dataset['generation_id']):
            raise ContractError('walk-forward trained model run/dataset lineage mismatch')
        review = ModelContractLoader(job.data_root).load(
            'model_quality_report', job.data_root / 'external_quality_reviews'
            / f"{run['quality_report_checksum_sha256']}.json")
        from ..contracts.model_layer import reviewed_allowed_statuses
        if (review['binding_type'] != 'model_run_v1' or review['bound_generation_id'] != run['generation_id']
                or review['report_checksum_sha256'] != run['quality_report_checksum_sha256']
                or review['status'] not in reviewed_allowed_statuses('model_run_v1')):
            raise ContractError('walk-forward model run review mismatch')
        artifact = ModelContractLoader(job.data_root).load(
            'model_artifact', job.data_root / artifact_binding['physical_path'] / 'manifest.json')
        if (artifact['generation_id'] != artifact_binding['generation_id']
                or artifact['manifest_digest_sha256'] != artifact_binding['manifest_digest_sha256']
                or artifact['model_run_content_generation_id'] != run_binding['generation_id']):
            raise ContractError('walk-forward artifact/model run lineage mismatch')
        verified_predictions = {item['generation_id']: item for item in ic_inputs['predictions']}
        for binding in bindings['prediction_set']:
            prediction = ModelContractLoader(job.data_root).load(
                'prediction_set', job.data_root / binding['physical_path'] / 'manifest.json')
            if (prediction['model_artifact_generation_id'] != artifact_binding['generation_id']
                    or prediction['model_run_generation_id'] != run_binding['generation_id']
                    or verified_predictions[binding['generation_id']]['manifest_digest_sha256']
                    != binding['manifest_digest_sha256']):
                raise ContractError('walk-forward prediction/trained artifact lineage mismatch')
        backtest_binding = one('backtest_result')
        backtest, _config, _artifacts = _verified_result(job.data_root, backtest_binding['generation_id'])
        if (backtest['manifest_digest_sha256'] != backtest_binding['manifest_digest_sha256']
                or _config['generation_id'] != job.request['backtest_config_binding']['generation_id']):
            raise ContractError('walk-forward backtest/config lineage mismatch')
        decision_dates = [binding['decision_date'] for binding in backtest['target_weight_bindings']]
        if decision_dates != fold['test_dates']:
            raise ContractError('walk-forward backtest target dates differ from protocol')
        return {
            'fold_id': fold['fold_id'], 'status': 'passed',
            'request_content_generation_id': job.request['request_content_generation_id'],
            'run_id': job.request['run_id'], 'result_content_generation_id': outcome['result_content_generation_id'],
            'result_manifest_digest_sha256': outcome['manifest_digest_sha256'],
            'data_root': str(job.data_root), 'test_dates': dates,
            'dataset_generation_id': dataset['generation_id'], 'ordered_features': dataset['ordered_features'],
            'factor_set': dataset['factor_set'], 'factor_version': dataset['factor_version'],
            'label_set_name': dataset['label_set_name'], 'prediction_bindings': predictions,
            'backtest_generation_id': backtest['generation_id'],
            'backtest_manifest_digest_sha256': backtest['manifest_digest_sha256'],
            'capital_semantics': 'independent_fold_backtest; no_stitched_pnl',
        }


def run_walk_forward(
    plan: dict, jobs: list[ResearchFoldJob], *, executor: FoldExecutor,
    store: WalkForwardStore, allow_short_diagnostic: bool = False,
) -> tuple[dict, Path]:
    """Preflight all requests; stop on first failure and preserve verified folds.

    Existing single-request ledgers remain the resume authority. A retry does
    not edit a ledger, move an artifact or invent a new run id to force fitting.
    """
    verify_walk_forward_plan(plan)
    if type(allow_short_diagnostic) is not bool:
        raise ContractError('allow_short_diagnostic must be boolean')
    if not plan['qualification']['one_year_coverage_met'] and not allow_short_diagnostic:
        raise ContractError('walk-forward calendar does not provide at least one year of OOS coverage')
    validate_fold_requests(plan, [job.request for job in jobs])
    executor.preflight(plan, jobs)
    store.publish_plan(plan)
    records, features, current_fold = [], None, None
    try:
        for fold, job in zip(plan['folds'], jobs):
            current_fold = {
                'fold_id': fold['fold_id'], 'request_content_generation_id': job.request['request_content_generation_id'],
                'run_id': job.request['run_id'], 'data_root': str(job.data_root),
            }
            outcome = executor.execute(job)
            record = executor.verify(fold, job, outcome)
            actual = {key: record[key] for key in
                      ('ordered_features', 'factor_set', 'factor_version', 'label_set_name')}
            if features is not None and canonical_json(actual) != canonical_json(features):
                raise ContractError('walk-forward actual features/factor/label semantics drift across folds')
            features = actual
            records.append(record)
    except BaseException as exc:
        try:
            store.publish_attempt(plan, records, status='failed', error_type=type(exc).__name__,
                                  failed_fold=current_fold)
        except (OSError, ContractError) as evidence_error:
            exc.add_note(f'walk-forward attempt persistence failed: {type(evidence_error).__name__}')
        raise
    path = store.publish_attempt(plan, records, status='passed', error_type=None)
    return {'status': 'folds_passed', 'plan_generation_id': plan['generation_id'],
            'folds': len(records), 'one_year_coverage_met': plan['qualification']['one_year_coverage_met'],
            'capital_semantics': plan['capital_semantics'],
            'trading_acceptance': 'not_certified_by_fold_execution'}, path
