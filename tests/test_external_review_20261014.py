"""Regression locks for the third review round (2026-10-14).

The 2026-10-13 round closed the symlink-guard family (B9) by scoping the rule to
each store root, but three sites were left behind -- and every one of them passed
the suite, because ``pytest`` hands out an already resolved ``tmp_path``:

* **B9-A** -- ``UniverseSnapshotReader.read`` still walked *every* lexical
  ancestor, so the research chain's main universe read refused a legitimate
  snapshot whenever the store root was spelled through a symlink (macOS ``/tmp``
  and ``$TMPDIR``, or any symlinked project directory);
* **B9-B** -- ``ModelContractLoader`` without ``accepted_root`` still did the
  same, and its two production callers (the run-state readback and the Qlib
  receipt readback) constructed it without one;
* **B9-C** -- ``AdjustedPriceDatasetStore`` handed ``reject_symlinks_within`` a
  pre-resolved path beside an unresolved root.  The lexical containment test read
  that as "path is outside the governed root" and broke a read that had worked
  *before* the B9 fix.

A first cut relaxed the guard to accept *either* spelling, which reopened the
hole it exists to close: a resolved path has already collapsed the in-store link,
so walking it always passed.  **B9-D** -- the guard now demands a consistent
spelling and the caller re-spells its resolved path onto the store root.

Every test here builds its root through a real symlink, so the guard is exercised
the way a user hits it rather than the way ``tmp_path`` presents it, and each one
also asserts the refusal that must survive the relaxation.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from tests.test_model_layer_contracts import _extended_document
from tests.test_research_chain_phase5 import DIGEST, RUNNER, _request, _runner
from uq.contracts.artifacts import QualityReportStore, UniverseSnapshotStore, UniverseSnapshotReader
from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from uq.contracts.model_layer import ModelContractLoader, model_manifest_identities
from uq.contracts.paths import reject_symlinks_within
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.research_chain.owning_contracts import AdjustedPriceDatasetStore
from uq.research_chain.resolver import ResolvedExecutionPlan, ResolvedStageBinding


def _symlinked_store_root(tmp_path: Path) -> Path:
    """A store root spelled through a symlink, the way macOS spells ``/tmp``."""
    real = tmp_path / "real-store"
    real.mkdir()
    link = tmp_path / "link-store"
    link.symlink_to(real, target_is_directory=True)
    return link


def _universe_document() -> dict:
    return {
        "universe_version": 1,
        "universe_id": "a500",
        "source": "reviewed fixture",
        "snapshot_time": "2025-09-01T15:00:00+08:00",
        "visibility_time": "2025-09-02T15:00:00+08:00",
        "valid_from": "2025-09-02",
        "valid_to": None,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
        "membership_evidence": "third-review regression fixture",
    }


def _publish_adjusted_price(root: Path) -> str:
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.2], "low": [9.8], "close": [10.0],
        "volume": [10000.0], "amount": [100000.0],
    })
    store = CanonicalV2Store(root)
    generation = store.prepare_generation(schema, date(2026, 8, 21), frame, {}, {})
    report_directory = QualityReportStore().save(root, {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [
            {"name": "coverage", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
        ],
        "errors": [],
        "warnings": [],
    })
    checksum = file_sha256_bytes((report_directory / "report.json").read_bytes())
    store.publish(schema, date(2026, 8, 21), frame, {}, {}, quality_checksum=checksum)
    return generation


def test_universe_snapshot_reads_through_a_symlinked_store_root(tmp_path: Path) -> None:
    """B9-A: the chain's main universe read must not refuse a legitimate snapshot."""
    root = _symlinked_store_root(tmp_path)
    directory = UniverseSnapshotStore().save(
        root, _universe_document(), pd.DataFrame({"instrument": ["600000.XSHG", "000001.XSHE"]})
    )
    # Precondition: the old guard walked exactly these parents, so this root is
    # the shape that used to fail.  Without the assertion the test could pass on a
    # tree where nothing was ever wrong.
    assert any(parent.is_symlink() for parent in directory.parents)

    frame = UniverseSnapshotReader(root).read(
        "a500", directory.name,
        requested_valid_from=date(2025, 9, 2), requested_valid_to=date(2025, 9, 5),
    )
    assert frame["instrument"].tolist() == ["600000.XSHG", "000001.XSHE"]


def test_adjusted_price_dataset_reads_through_a_symlinked_store_root(tmp_path: Path) -> None:
    """B9-C: the store root is spelled through a link and the read still works."""
    root = _symlinked_store_root(tmp_path)
    generation = _publish_adjusted_price(root)
    manifest = AdjustedPriceDatasetStore(root).read_manifest(generation)
    assert manifest["generation_id"] == generation


def test_reject_symlinks_within_accepts_a_consistently_spelled_pair(tmp_path: Path) -> None:
    """B9-C/B9-D: a pair spelled the same way is accepted; a mixed one is not."""
    root = _symlinked_store_root(tmp_path)
    (root / "sub").mkdir()
    resolved = (root / "sub").resolve()
    # Both spelled through the link, or both resolved: the walk can descend the
    # spelled components either way.
    assert reject_symlinks_within(root, root / "sub") == resolved
    assert reject_symlinks_within(root.resolve(), resolved) == resolved
    # B9-D: a resolved path beside an unresolved root is the hole that let an
    # in-store link through -- the resolved form no longer spells the link, so
    # the guard refuses the pair instead of silently walking past it.
    with pytest.raises(ContractError, match="outside the governed root"):
        reject_symlinks_within(root, resolved)


def test_reject_symlinks_within_refuses_a_resolved_path_that_hid_a_link(tmp_path: Path) -> None:
    """B9-D: the exact smuggling shape the "either spelling" fallback accepted."""
    root = _symlinked_store_root(tmp_path)
    (root / "canonical" / "real").mkdir(parents=True)
    (root / "canonical" / "link").symlink_to(root / "canonical" / "real", target_is_directory=True)
    # A caller that resolved before calling handed in the link's *target*: the
    # resolved spelling is identical to the legitimate ``canonical/real`` path,
    # so only a consistent-spelling rule can tell the two apart.
    with pytest.raises(ContractError, match="outside the governed root"):
        reject_symlinks_within(root, (root / "canonical" / "link").resolve())
    # Spelled consistently, the very same link is caught.
    with pytest.raises(ContractError, match="symbolic links are forbidden"):
        reject_symlinks_within(root, root / "canonical" / "link")


def test_reject_symlinks_within_still_refuses_a_link_inside_the_store(tmp_path: Path) -> None:
    """The relaxation is about spelling, never about accepting an in-store link."""
    root = tmp_path / "store"
    (root / "real").mkdir(parents=True)
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    with pytest.raises(ContractError, match="symbolic links are forbidden"):
        reject_symlinks_within(root, root / "link")
    with pytest.raises(ContractError, match="outside the governed root"):
        reject_symlinks_within(root, (tmp_path / "elsewhere").resolve())
    # Lexically inside the root, but ``..`` walks back out of it.
    with pytest.raises(ContractError, match="leaves the governed root"):
        reject_symlinks_within(root, root / "real" / ".." / ".." / "escape")


def test_run_state_readback_scopes_the_loader_to_the_run_store(tmp_path: Path) -> None:
    """B9-B: the state lives in this run's store, so that store is the scope."""
    root = _symlinked_store_root(tmp_path)
    request = _request()
    runner = _runner(root)
    plan = ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
        stage_plan_sha256=request["stage_plan_sha256"],
        stage_bindings=(),
        resolved_execution_plan_sha256=DIGEST,
    )
    state_path = runner.dry_run(
        plan, runner_identity=RUNNER, created_at=request["created_at"]
    )["manifest_path"]
    assert any(parent.is_symlink() for parent in state_path.parents)

    rebuilt = runner._plan_with_stage_outputs(plan, state_path)
    assert [binding.stage for binding in rebuilt.stage_bindings] == ["resolve_request"]

    # The unscoped loader is the trap this call site must not fall back into.
    with pytest.raises(ContractError, match="symbolic links are forbidden in accepted contracts"):
        ModelContractLoader().load("research_run_state", state_path)


def _receipt_document() -> dict:
    document = _extended_document("qlib_init_receipt")
    document["contract_version"] = 2
    document.pop("run_id", None)
    document.pop("created_at", None)
    generation, digest = model_manifest_identities(
        document,
        schema_name="qlib_init_receipt",
        exclude_fields={"quality_report_checksum_sha256", "resolved_provider_uri_sha256", "cache_root"},
        digest_exclude_fields={"export_manifest_digest_sha256", "resolved_provider_uri_sha256", "cache_root"},
    )
    document["generation_id"] = generation
    document["manifest_digest_sha256"] = digest
    return document


def test_qlib_receipt_readback_scopes_the_loader_to_the_run_store(tmp_path: Path) -> None:
    """B9-B: the second production caller of the unscoped loader was a trap too."""
    root = _symlinked_store_root(tmp_path)
    receipt = _receipt_document()
    receipt_dir = root / "external_quality_reviews"
    receipt_dir.mkdir()
    receipt_path = receipt_dir / f"{receipt['generation_id']}.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert any(parent.is_symlink() for parent in receipt_path.parents)

    runner = _runner(root)
    binding = ResolvedStageBinding(
        stage="qlib_export",
        output_family="qlib_init_receipt",
        generation_id=receipt["generation_id"],
        manifest_digest_sha256=receipt["manifest_digest_sha256"],
        data_checksum_sha256="0" * 64,
    )
    assert runner._read_receipt(binding)["generation_id"] == receipt["generation_id"]

    # Reverting the call site to an unscoped loader must fail here.
    with pytest.raises(ContractError, match="symbolic links are forbidden in accepted contracts"):
        ModelContractLoader().load("qlib_init_receipt", receipt_path)
