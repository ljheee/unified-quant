from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from uq.contracts.model_layer import sha256_json
from uq.errors import ContractError
from uq.research_chain.provenance import capture_environment, verified_runner_identity
from uq.research_chain.telemetry import RunTelemetry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    for directory in ('src/uq', 'config', 'scripts'):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / 'src/uq/component.py').write_text('VERSION = 1\n')
    (tmp_path / 'config/policy.json').write_text('{}\n')
    (tmp_path / 'scripts/run.sh').write_text('true\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "test"\n')
    (tmp_path / 'uv.lock').write_text('version = 1\n')
    for command in (
        ['git', 'init', '-q', str(tmp_path)],
        ['git', '-C', str(tmp_path), 'add', '.'],
        ['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c',
         'user.email=test@example.invalid', 'commit', '-qm', 'fixture'],
    ):
        subprocess.run(command, check=True, capture_output=True)
    return tmp_path


def test_fingerprint_binds_dirty_code_and_config_but_not_artifacts(checkout):
    initial = capture_environment(checkout)
    assert initial == capture_environment(checkout)
    assert initial['code_fingerprint'] != '0' * 64
    (checkout / 'src/uq/component.py').write_text('VERSION = 2\n')
    changed = capture_environment(checkout)
    assert changed['git_head'] == initial['git_head']
    assert changed['code_fingerprint'] != initial['code_fingerprint']
    (checkout / 'config/policy.json').write_text('{"cap": 0.1}\n')
    configured = capture_environment(checkout)
    assert configured['code_fingerprint'] != changed['code_fingerprint']
    (checkout / 'evidence').mkdir()
    (checkout / 'evidence/result.json').write_text('{}\n')
    (checkout / 'README.md').write_text('new documentation\n')
    assert capture_environment(checkout)['code_fingerprint'] == configured['code_fingerprint']
    (checkout / 'uv.lock').write_text('version = 2\n')
    relocked = capture_environment(checkout)
    assert relocked['environment_lock_digest_sha256'] != initial['environment_lock_digest_sha256']
    assert relocked['code_fingerprint'] == configured['code_fingerprint']


@pytest.mark.parametrize('field', ['code_fingerprint', 'environment_lock_digest_sha256'])
def test_zero_or_mismatched_bindings_are_refused(checkout, field):
    observed = capture_environment(checkout)
    identity = verified_runner_identity(observed, observed)
    assert identity['code_fingerprint'] == observed['code_fingerprint']
    assert identity['lock_digest_sha256'] == observed['environment_lock_digest_sha256']
    with pytest.raises(ContractError, match=field):
        verified_runner_identity({**observed, field: '0' * 64}, observed)


def test_source_symlinks_are_refused_but_checkout_ancestors_may_be_links(checkout, tmp_path):
    alias = tmp_path.parent / (tmp_path.name + '-alias')
    alias.symlink_to(checkout, target_is_directory=True)
    try:
        assert capture_environment(alias / '.')['code_fingerprint'] == capture_environment(checkout)['code_fingerprint']
    finally:
        alias.unlink()
    target = checkout / 'src/uq/component.py'
    target.rename(checkout / 'actual.py')
    target.symlink_to(checkout / 'actual.py')
    with pytest.raises(ContractError, match='symbolic links'):
        capture_environment(checkout)


def test_telemetry_preserves_failed_attempt_and_does_not_invent_reuse_timings(tmp_path):
    request = {'request_content_generation_id': 'a' * 64,
               'run_id': '00000000-0000-4000-8000-000000000011'}
    attempt = RunTelemetry(tmp_path, request, {'code_fingerprint': 'b' * 64})
    with pytest.raises(ContractError, match='export failed'):
        with attempt:
            attempt.reused('factor_computation')
            with attempt.stage('qlib_export'):
                raise ContractError('export failed')
    record = json.loads(attempt.path.read_text())
    checksum = record.pop('checksum_sha256')
    assert sha256_json(record) == checksum
    assert record['status'] == 'failed'
    assert record['stages'][0] == {'stage': 'factor_computation', 'status': 'reused'}
    assert record['stages'][1]['status'] == 'failed'
    assert record['stages'][1]['error_type'] == 'ContractError'
    assert record['stages'][1]['elapsed_seconds'] >= 0
    before = attempt.path.read_bytes()
    with RunTelemetry(tmp_path, request, {}) as retry:
        with retry.stage('qlib_export'):
            pass
    assert retry.path != attempt.path
    assert attempt.path.read_bytes() == before
    assert json.loads(retry.path.read_text())['status'] == 'passed'


def test_telemetry_store_allows_root_alias_and_rejects_links_inside(tmp_path):
    request = {'request_content_generation_id': 'a' * 64,
               'run_id': '00000000-0000-4000-8000-000000000011'}
    root = tmp_path / 'store'
    root.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    with RunTelemetry(alias, request, {}) as attempt:
        pass
    assert attempt.path.is_file()
    outside = tmp_path / 'outside'
    outside.mkdir()
    # A different store whose governed research_runs entry is a link is refused.
    bad_root = tmp_path / 'bad-store'
    bad_root.mkdir()
    (bad_root / 'research_runs').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError, match='symbolic links'):
        RunTelemetry(bad_root, request, {})


def _fake_cli_runtime(monkeypatch):
    from uq.research_chain import cli
    observed = capture_environment(ROOT)
    called = {}

    def execute(plan, **kwargs):
        called.update(kwargs)
        with kwargs['telemetry'].stage('model_training'):
            pass
        return SimpleNamespace(result_manifest={
            'result_content_generation_id': 'd' * 64,
            'manifest_digest_sha256': 'e' * 64, 'final_status': 'passed',
        }, result_path=Path('result.json'))

    runner = SimpleNamespace(
        factor_adapter=SimpleNamespace(store=None),
        dataset_adapter=SimpleNamespace(universe_store=None, adjusted_price_store=None,
                                       label_store=None, preprocessing_store=None),
        backtest_adapter=SimpleNamespace(backtest_config_store=None), execute=execute,
    )
    monkeypatch.setattr(cli, '_build_runner', lambda **kwargs: runner)
    monkeypatch.setattr(cli, 'ResearchChainRequestResolver', lambda *args, **kwargs:
                        SimpleNamespace(resolve=lambda *args, **kwargs: 'resolved-plan'))
    return observed, called


def test_cli_executes_with_observed_identity_and_emits_receipt(tmp_path, monkeypatch):
    from uq.research_chain.cli import _execute
    observed, called = _fake_cli_runtime(monkeypatch)
    request = {'contract_version': 1, 'environment': observed,
               'request_content_generation_id': 'a' * 64,
               'run_id': '00000000-0000-4000-8000-000000000011'}
    report = _execute(request, decisions_document={}, scores_document=None,
                      provider_document={'provider_id': 'external-model-quality-reviewer-v1',
                                         'provider_command': ['unused-reviewer']},
                      project_root=ROOT, data_root=tmp_path)
    assert called['runner_identity'] == verified_runner_identity(observed, observed)
    receipt = json.loads(Path(report['telemetry_path']).read_text())
    assert receipt['environment'] == observed
    assert receipt['stages'][0]['stage'] == 'model_training'


def test_cli_rejects_placeholder_identity_before_training(tmp_path, monkeypatch):
    from uq.research_chain.cli import _execute
    observed, called = _fake_cli_runtime(monkeypatch)
    request = {'contract_version': 1, 'environment': {**observed, 'code_fingerprint': '0' * 64}}
    with pytest.raises(ContractError, match='does not match observed bytes'):
        _execute(request, decisions_document={}, scores_document=None,
                 provider_document={'provider_id': 'external-model-quality-reviewer-v1',
                                    'provider_command': ['unused-reviewer']},
                 project_root=ROOT, data_root=tmp_path)
    assert not called
    assert not (tmp_path / 'research_runs/results').exists()


def test_cli_static_decisions_reach_execution_without_command_provider(tmp_path, monkeypatch):
    from uq.research_chain.cli import _execute
    observed, called = _fake_cli_runtime(monkeypatch)
    request = {'contract_version': 1, 'environment': observed,
               'request_content_generation_id': 'a' * 64,
               'run_id': '00000000-0000-4000-8000-000000000011'}
    raw = {'model_run': {'raw-reviewed-decision': True}}
    _execute(request, decisions_document={
        'quality_decisions': raw, 'request_quality_decisions': {},
    }, scores_document=None, project_root=ROOT, data_root=tmp_path)
    assert called['quality_decisions'] == raw
