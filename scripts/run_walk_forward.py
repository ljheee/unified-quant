#!/usr/bin/env python3
"""Plan or execute a fixed, reviewed multi-fold research protocol.

Each execute job references an already-reviewed research request plus the same
external-reviewer documents the single-request CLI accepts. It does not create
upstream data or reviews, accept external scores, or stitch independent PnL.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.walk_forward import WalkForwardSpec, WalkForwardStore, plan_from_published_calendar
from uq.evaluation.walk_forward_runner import ResearchFoldExecutor, ResearchFoldJob, run_walk_forward


def _json(path: str | Path) -> dict:
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(document, dict):
        raise ContractError(f'walk-forward document must be an object: {path}')
    return document


def _jobs(path: Path) -> list[ResearchFoldJob]:
    items = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(items, list) or not items:
        raise ContractError('walk-forward jobs must be a nonempty list')
    jobs = []
    required = {'request_json', 'data_root', 'quality_decisions_json'}
    optional = {'decision_provider_json', 'membership_root', 'market_dataset_root', 'paper_risk_documents_json'}
    for item in items:
        if not isinstance(item, dict) or required - set(item) or set(item) - required - optional:
            raise ContractError('walk-forward job fields mismatch; external scores are not accepted')
        if not all(isinstance(value, str) and value for value in item.values()):
            raise ContractError('walk-forward job paths must be nonempty strings')
        jobs.append(ResearchFoldJob(
            request=_json(item['request_json']), data_root=Path(item['data_root']),
            decisions_document=_json(item['quality_decisions_json']),
            provider_document=_json(item['decision_provider_json']) if 'decision_provider_json' in item else None,
            membership_root=Path(item['membership_root']) if 'membership_root' in item else None,
            market_dataset_root=Path(item['market_dataset_root']) if 'market_dataset_root' in item else None,
            paper_risk_documents_path=Path(item['paper_risk_documents_json'])
                                      if 'paper_risk_documents_json' in item else None,
        ))
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('plan', 'execute'), required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--calendar-root', type=Path)
    parser.add_argument('--calendar-generation')
    parser.add_argument('--calendar-checksum')
    parser.add_argument('--spec-json', type=Path)
    parser.add_argument('--plan-generation')
    parser.add_argument('--jobs-json', type=Path)
    parser.add_argument('--project-root', type=Path)
    parser.add_argument('--allow-short-diagnostic', action='store_true')
    args = parser.parse_args(argv)
    try:
        store = WalkForwardStore(args.output_root)
        if args.mode == 'plan':
            if any(value is None for value in (args.calendar_root, args.calendar_generation,
                                              args.calendar_checksum, args.spec_json)):
                raise ContractError('plan mode requires published calendar root/generation/checksum and spec JSON')
            plan = plan_from_published_calendar(args.calendar_root, args.calendar_generation,
                                                args.calendar_checksum, WalkForwardSpec(**_json(args.spec_json)))
            path = store.publish_plan(plan)
            outcome = {'status': 'plan_published', 'generation_id': plan['generation_id'],
                       'folds': len(plan['folds']), 'qualification': plan['qualification']}
        else:
            if any(value is None for value in (args.plan_generation, args.jobs_json, args.project_root)):
                raise ContractError('execute mode requires plan generation, jobs JSON and project root')
            plan = store.read_plan(args.plan_generation)
            jobs = _jobs(args.jobs_json)
            outcome, path = run_walk_forward(
                plan, jobs, executor=ResearchFoldExecutor(args.project_root), store=store,
                allow_short_diagnostic=args.allow_short_diagnostic,
            )
        print(json.dumps({**outcome, 'evidence_path': str(path)}, sort_keys=True))
        return 0
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'walk_forward_rejected', 'errors': [str(exc)]}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
