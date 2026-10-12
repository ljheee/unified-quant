"""Partition-set binding resolver for paper execution template v2.

Recomputes ``set_digest_sha256`` from governed store partitions and compares
against the bound value, sharing the same digest convention as the v4
``index_membership_binding`` and ``factor_partition_set_binding``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Protocol, Sequence

from ..errors import ContractError
from ..contracts.manifest import canonical_json
import hashlib


class PartitionSetReader(Protocol):
    def list_partitions(self, **kwargs: Any) -> Sequence[Mapping[str, Any]]: ...


def set_digest_sha256(triples: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 over canonical JSON of the ordered partition-set triples."""
    payload = [{"session_or_decision_date": t["session_or_decision_date"], "generation_id": t["generation_id"], "data_checksum_sha256": t["data_checksum_sha256"]} for t in triples]
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def verify_partition_set_binding(
    binding: Mapping[str, Any],
    triples: Sequence[Mapping[str, Any]],
    *,
    family: str,
) -> None:
    """Recompute the set digest from ordered triples and compare against the binding."""
    if binding.get("family") != family:
        raise ContractError(f"partition set binding family mismatch: expected {family!r}, got {binding.get('family')!r}")
    if int(binding.get("member_count", 0)) != len(triples):
        raise ContractError(
            f"partition set member count mismatch for {family}: expected {binding.get('member_count')}, got {len(triples)}"
        )
    window_start = binding.get("window_start")
    window_end = binding.get("window_end")
    for triple in triples:
        session_date = triple.get("session_or_decision_date")
        if isinstance(session_date, str):
            session_date = date.fromisoformat(session_date)
        if session_date < date.fromisoformat(window_start) or session_date > date.fromisoformat(window_end):
            raise ContractError(
                f"partition set member date {session_date} outside binding window [{window_start}, {window_end}] for {family}"
            )
    expected = binding.get("set_digest_sha256")
    actual = set_digest_sha256(triples)
    if expected != actual:
        raise ContractError(f"partition set digest mismatch for {family}")


def build_set_binding(
    triples: Sequence[Mapping[str, Any]],
    *,
    family: str,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """Build a partition-set binding from ordered triples."""
    if not triples:
        raise ContractError(f"cannot build partition set binding for {family}: no triples")
    return {
        "family": family,
        "window_start": window_start,
        "window_end": window_end,
        "set_digest_sha256": set_digest_sha256(triples),
        "member_count": len(triples),
    }
