#!/usr/bin/env python3
"""Publish an immutable, independently reviewed PIT factor exposure panel.

The input is an offline Parquet file with columns ``instrument``, ``datetime``,
one or more controls (industry, market_cap, liquidity), and timezone-aware
``available_at``. The publisher validates row keys and requires each exposure
to have been visible by the 15:00 Asia/Shanghai session close. It does not fetch,
approve, or invent a source. A source approval reference and an external quality
decision are mandatory.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

from uq.errors import ContractError
from uq.research_chain.owning_contracts import FactorExposureStore


def _decision_provider(path: Path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"unreadable external quality decision: {exc}") from exc
    if isinstance(document, dict) and "provider_command" in document:
        command = document["provider_command"]
        if (
            document.get("provider_id") != "external-model-quality-reviewer-v1"
            or not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ContractError("invalid external quality decision provider configuration")

        def provide(generation_id: str) -> dict[str, Any]:
            completed = subprocess.run(
                [
                    *command,
                    "--binding-type", "factor_exposure_panel_v1",
                    "--subject-generation-id", generation_id,
                    "--subject-manifest-digest-sha256", generation_id,
                    "--output-family", "factor_exposure_panel",
                    "--provider-config-ref", document.get(
                        "provider_config_ref", "factor-exposure-publisher"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ContractError(f"external exposure review provider failed: {detail}")
            try:
                decision = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise ContractError("external exposure review provider returned malformed JSON") from exc
            if not isinstance(decision, dict):
                raise ContractError("external exposure review provider must return an object")
            return decision

        return provide
    if isinstance(document, dict) and isinstance(document.get("quality_decisions"), dict):
        decision = document["quality_decisions"].get("factor_exposure_panel_v1")
        if decision is None:
            raise ContractError("quality decisions document has no factor_exposure_panel_v1 entry")
    else:
        decision = document
    if not isinstance(decision, dict):
        raise ContractError("external quality decision must be an object")
    return lambda _generation_id: decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-parquet", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-approval-ref", required=True)
    parser.add_argument("--control", action="append", required=True,
                        choices=("industry", "market_cap", "liquidity"))
    parser.add_argument("--quality-decisions-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.input_parquet.is_file():
            raise ContractError("factor exposure input Parquet is missing")
        frame = pd.read_parquet(args.input_parquet)
        decision = _decision_provider(args.quality_decisions_json)
        manifest = FactorExposureStore(args.store_root).publish(
            frame,
            controls=args.control,
            source_id=args.source_id,
            source_approval_ref=args.source_approval_ref,
            quality_decision=decision,
        )
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"status": "publication_rejected", "error": str(exc)}, ensure_ascii=False))
        return 2
    report = {
        "record_version": 1,
        "kind": "factor_exposure_panel_publication",
        "generation_id": manifest["generation_id"],
        "manifest_digest_sha256": manifest["manifest_digest_sha256"],
        "data_checksum_sha256": manifest["data_checksum_sha256"],
        "source_id": manifest["source_id"],
        "source_approval_ref": manifest["source_approval_ref"],
        "controls": manifest["controls"],
        "coverage": [manifest["coverage_start_date"], manifest["coverage_end_date"]],
        "rows": manifest["row_count"],
        "signed_by_publisher": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "published", **report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
