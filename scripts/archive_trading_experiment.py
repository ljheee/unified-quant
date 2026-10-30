#!/usr/bin/env python3
"""Bind a passed research run, telemetry and costed evaluation into an archive.

This read-side command does not train, ingest, sign reviews or approve trading.
Source artifacts must already have been published through their owning stores.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.experiment_archive import ExperimentArchiveStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--benchmark-root', type=Path, required=True)
    parser.add_argument('--evaluation-root', type=Path, required=True)
    parser.add_argument('--archive-root', type=Path, required=True)
    parser.add_argument('--result-generation', required=True)
    parser.add_argument('--result-digest', required=True)
    parser.add_argument('--evaluation-generation', required=True)
    parser.add_argument('--telemetry-path', type=Path, required=True)
    parser.add_argument('--environment-json', type=Path, required=True)
    parser.add_argument('--limitations-json', type=Path, required=True)
    parser.add_argument('--universe', required=True)
    parser.add_argument('--oos-start', required=True)
    parser.add_argument('--oos-end', required=True)
    parser.add_argument('--protocol', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--variant', required=True)
    args = parser.parse_args(argv)
    try:
        environment = json.loads(args.environment_json.read_text(encoding='utf-8'))
        limitations = json.loads(args.limitations_json.read_text(encoding='utf-8'))
        store = ExperimentArchiveStore(args.archive_root)
        record = store.publish(
            components={
                'universe': args.universe, 'oos_start': args.oos_start,
                'oos_end': args.oos_end, 'protocol': args.protocol,
                'model': args.model, 'variant': args.variant,
            },
            data_root=args.data_root, benchmark_root=args.benchmark_root,
            evaluation_root=args.evaluation_root,
            result_generation=args.result_generation, result_digest=args.result_digest,
            evaluation_generation=args.evaluation_generation,
            telemetry_path=args.telemetry_path, environment=environment,
            limitations=limitations,
        )
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'archive_rejected', 'errors': [str(exc)]}, sort_keys=True))
        return 2
    print(json.dumps({'status': 'archived', 'experiment_name': record['experiment_name'],
                      'manifest_path': str(store.path_for(record['experiment_name'])),
                      'archive_checksum_sha256': record['archive_checksum_sha256']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
