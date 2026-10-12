from __future__ import annotations

import argparse
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from uq.contracts.manifest import canonical_json
from uq.contracts.model_layer import (
    research_stage_plan_v3_sha256,
    research_stage_plan_v4_sha256,
    research_stage_plan_v5_sha256,
)

_SIGNERS = {
    3: ("research_stage_plan_v3_activation", "research-stage-plan-reviewer-v3-test-2026-10"),
    4: ("research_stage_plan_v4_activation", "research-stage-plan-reviewer-v4-test-2026-10"),
    5: ("research_stage_plan_v5_activation", "research-stage-plan-reviewer-v5-test-2026-10"),
}
_DIGESTS = {
    3: research_stage_plan_v3_sha256,
    4: research_stage_plan_v4_sha256,
    5: research_stage_plan_v5_sha256,
}
TEST_SEED_HEX = "02" * 32


def build_review(stage_plan_sha256: str, reviewer: str, seed_hex: str, version: int) -> dict:
    review_type, key_id = _SIGNERS[version]
    unsigned = {
        "review_type": review_type,
        "subject_generation_id": stage_plan_sha256,
        "subject_manifest_digest_sha256": stage_plan_sha256,
        "review_status": "approved",
        "reviewer": reviewer,
        "key_id": key_id,
    }
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    return {**unsigned, "review_signature_sha256": key.sign(canonical_json(unsigned)).hex()}


def main() -> int:
    parser = argparse.ArgumentParser(description="sign a stage-plan activation review (test seed only)")
    parser.add_argument("--version", type=int, default=5, choices=[3, 4, 5])
    parser.add_argument("--reviewer", default="repository-owner")
    parser.add_argument("--output", type=Path, help="write review JSON to this path instead of stdout")
    args = parser.parse_args()
    review = build_review(_DIGESTS[args.version](), args.reviewer, TEST_SEED_HEX, args.version)
    text = json.dumps(review, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
