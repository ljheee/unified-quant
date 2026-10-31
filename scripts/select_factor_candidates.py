#!/usr/bin/env python3
"""Produce a configured validation-only factor shortlist from a screen report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.contracts.gate_contracts import validate_contract
from uq.errors import ContractError
from uq.evaluation.factor_screening import select_factor_candidates


def _read_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is missing or malformed") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screen-report", type=Path, required=True,
                        help="factor_screen_report.v1 JSON created for the validation window only")
    parser.add_argument("--policy-json", type=Path, required=True,
                        help="factor selection policy JSON")
    parser.add_argument("--out", type=Path, required=True,
                        help="new output path; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        report = _read_object(args.screen_report, "factor screen report")
        policy = _read_object(args.policy_json, "factor selection policy")
        if report.get("kind") == "tushare_factor_candidate_screen_report":
            validate_contract("tushare_factor_candidate_screen_report.v1.json", report)
        else:
            validate_contract("factor_screen_report.v1.json", report)
        if args.out.exists():
            raise ContractError("factor shortlist output already exists")
        shortlist = select_factor_candidates(report, policy=policy)
        validate_contract("factor_selection_shortlist.v1.json", shortlist)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(shortlist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"status": "selection_rejected", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "validation_shortlist_created",
        "generation_id": shortlist["generation_id"],
        "selected_candidates": shortlist["selected_candidates"],
        "publication_scope": shortlist["publication_scope"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
