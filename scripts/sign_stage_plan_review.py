from __future__ import annotations

import argparse
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from uq.contracts.manifest import canonical_json
from uq.contracts.model_layer import research_stage_plan_v4_sha256

REVIEW_TYPE = "research_stage_plan_v4_activation"
KEY_ID = "research-stage-plan-reviewer-v4-test-2026-10"
TEST_SEED_HEX = "02" * 32


def build_review(stage_plan_sha256: str, reviewer: str, seed_hex: str) -> dict:
    unsigned = {
        "review_type": REVIEW_TYPE,
        "subject_generation_id": stage_plan_sha256,
        "subject_manifest_digest_sha256": stage_plan_sha256,
        "review_status": "approved",
        "reviewer": reviewer,
        "key_id": KEY_ID,
    }
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    return {**unsigned, "review_signature_sha256": key.sign(canonical_json(unsigned)).hex()}


def main() -> int:
    parser = argparse.ArgumentParser(description="sign the v4 stage-plan activation review (test seed only)")
    parser.add_argument("--reviewer", default="repository-owner")
    parser.add_argument("--output", type=Path, help="write review JSON to this path instead of stdout")
    args = parser.parse_args()
    review = build_review(research_stage_plan_v4_sha256(), args.reviewer, TEST_SEED_HEX)
    text = json.dumps(review, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
