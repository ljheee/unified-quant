"""Runtime execution-mode governance for out-of-band trust material."""

from __future__ import annotations

import os

from .errors import ContractError


_TEST_ED25519_PUBLIC_KEY_HEXES = frozenset({
    "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29",
    "8a88e3dd7409f195fd52db2d3cba5d72ca6709bf1d94121bf3748801b40f6f5c",
    "8139770ea87d175f56a35466c34c7ecccb8d8a91b4ee37a25df60f5b8fc9b394",
})


def current_runtime_mode() -> str:
    mode = os.environ.get("UQ_RUNTIME_MODE", "research").strip().lower()
    if mode not in {"research", "production"}:
        raise ContractError(
            "UQ_RUNTIME_MODE must be 'research' or 'production'"
        )
    return mode


def is_production_runtime() -> bool:
    return current_runtime_mode() == "production"


def is_repository_test_review_key(public_key_hex: str) -> bool:
    return public_key_hex.lower() in _TEST_ED25519_PUBLIC_KEY_HEXES


def require_production_review_key(public_key_hex: str, *, context: str) -> None:
    if is_production_runtime() and is_repository_test_review_key(public_key_hex):
        raise ContractError(
            f"{context} cannot use the repository test-mode Ed25519 trust anchor in production"
        )
