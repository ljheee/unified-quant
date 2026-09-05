from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..contracts.model_layer import ModelContractLoader
from ..errors import ContractError
from .resolver import FileResearchRunStore
from .runner import ResearchChainRunner

_EXIT_CODES = {
    "dry_run_published": 0,
    "published": 0,
    "rejected": 3,
    "configuration_failure": 3,
    "quality_decision_missing": 3,
    "quality_decision_rejected": 3,
    "lineage_mismatch": 3,
    "input_unresolved": 3,
    "input_tampered": 3,
    "request_invalid": 3,
    "store_read_failed": 4,
    "publication_conflict": 5,
    "stage_failed": 4,
}


def uq_research_run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uq-research-run")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("dry-run", "execute"), required=True)
    parser.add_argument("--request-json", type=Path, required=True)
    parser.add_argument("--quality-decisions-json", type=Path)
    args = parser.parse_args(argv)

    try:
        args.data_root.mkdir(parents=True, exist_ok=True)
        request = json.loads(args.request_json.read_text(encoding="utf-8"))
        if args.mode == "dry-run":
            result = _dry_run(request, data_root=args.data_root)
        else:
            decisions_document = None
            if args.quality_decisions_json is None:
                raise ContractError("execute mode requires --quality-decisions-json")
            decisions_document = json.loads(args.quality_decisions_json.read_text(encoding="utf-8"))
            result = _execute(
                request,
                decisions_document=decisions_document,
                project_root=args.project_root,
                data_root=args.data_root,
            )
    except (ContractError, OSError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        status = getattr(exc, "reason", "configuration_failure")
        if status not in _EXIT_CODES:
            status = "configuration_failure"
        print(json.dumps({"status": status, "errors": [str(exc)]}, sort_keys=True))
        return _EXIT_CODES[status]
    print(json.dumps(result, sort_keys=True))
    return _EXIT_CODES[result["status"]]


def _dry_run(request: dict[str, Any], *, data_root: Path) -> dict[str, Any]:
    ModelContractLoader.validate("research_run_request", request)
    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    runner.factor_adapter = None
    runner.dataset_adapter = None
    runner.export_adapter = None
    runner.model_adapter = None
    runner.prediction_adapter = None
    runner.portfolio_adapter = None
    runner.backtest_adapter = None
    runner.run_store = FileResearchRunStore(data_root)
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Plan:
        request: dict[str, Any]
        request_manifest_digest_sha256: str

    plan = _Plan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
    )
    environment = request["environment"]
    state = runner.dry_run(
        plan,
        runner_identity={
            "code_fingerprint": environment["code_fingerprint"],
            "environment_profile": "locked",
            "lock_digest_sha256": environment["environment_lock_digest_sha256"],
        },
    )
    return {
        "status": "dry_run_published",
        "request_content_generation_id": request["request_content_generation_id"],
        "run_id": request["run_id"],
        "manifest_path": str(state["manifest_path"]),
        "manifest_digest_sha256": state["manifest_digest_sha256"],
    }


def _execute(
    request: dict[str, Any],
    *,
    decisions_document: dict[str, Any],
    project_root: Path,
    data_root: Path,
) -> dict[str, Any]:
    raise ContractError("execute provider wiring is not configured")


if __name__ == "__main__":
    raise SystemExit(uq_research_run())
