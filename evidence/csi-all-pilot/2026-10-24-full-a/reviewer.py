#!/usr/bin/env python3
"""Stand-in external reviewer for the full-A smoke run.

TEST KEY reviewer, as previous smoke rounds used. Artifacts it signs prove the
chain runs; they are NOT a research conclusion.

`factor_v1` is not in the model-quality registry: its quality report is owned by
the factor store, so the reviewer returns that stored report rather than signing
a new one.
"""
import argparse, json, sys
sys.path.insert(0, "/Users/lijianhua04/Documents/IdeaProject/unified-quant")
from tests.review_key import REVIEWER_PRIVATE_KEY
from uq.contracts.model_layer import (
    _MODEL_QUALITY_REVIEW_REGISTRY, create_reviewed_quality_decision)

parser = argparse.ArgumentParser()
parser.add_argument("--binding-type", required=True)
parser.add_argument("--subject-generation-id", required=True)
parser.add_argument("--subject-manifest-digest-sha256", default="")
parser.add_argument("--output-family", default="")
parser.add_argument("--provider-config-ref", default="")
args = parser.parse_args()

if args.binding_type == "factor_v1":
    # The factor store owns this report; hand back exactly what it published.
    from pathlib import Path
    import os
    root = Path(os.environ.get("UQ_DATA_ROOT", "/Users/lijianhua04/Documents/IdeaProject/unified-quant/data/full-a-root"))
    path = root / "reports" / "factor_v1" / args.subject_generation_id / "report.json"
    if not path.is_file():
        sys.stderr.write(f"factor quality report missing: {path}\n")
        sys.exit(1)
    stored = json.loads(path.read_text())
    print(json.dumps({
        "contract_version": 1, "schema_version": "1.0.0",
        "binding_type": "factor_v1",
        "subject_generation_id": args.subject_generation_id,
        "subject_manifest_digest_sha256": stored.get("subject_content_sha256"),
        "owning_report": stored,
        "decision_checksum_sha256": __import__(
            "uq.contracts.model_layer", fromlist=["x"]
        ).validate_quality_decision_owning_report(stored),
        "provider_id": "external-model-quality-reviewer-v1",
        "trust_anchor_id": "factor-review-key-v1",
    }))
    sys.exit(0)

registry = _MODEL_QUALITY_REVIEW_REGISTRY.bindings[args.binding_type]
print(json.dumps(create_reviewed_quality_decision(
    binding_type=args.binding_type, policy=registry["policy"], status="passed",
    checks=[{"name": n, "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
            for n in registry["allowed_checks"]],
    errors=[], warnings=[], producer_code_fingerprint="0" * 64,
    private_key_pem=REVIEWER_PRIVATE_KEY,
    subject_generation_id=args.subject_generation_id,
    subject_content_sha256=(args.subject_manifest_digest_sha256 or args.subject_generation_id))))
