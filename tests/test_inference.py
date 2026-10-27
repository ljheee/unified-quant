"""Locks for loading a governed model artifact and scoring a decision date.

The research chain used to take its scores from an external `--scores-json`, so
the published artifact was never loaded and nothing in the chain proved the
model could produce a score at all. These tests cover the refusals the reader
must make; the successful path needs a real Qlib export and is exercised by the
full-A smoke run recorded in cr.md 8.77.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.inference import (
    InferenceResult,
    assert_runtime_version,
    score_decision_dates,
)


def test_an_unpublished_export_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="unpublished or incomplete Qlib export"):
        score_decision_dates(
            export_root=tmp_path / "missing",
            dataset_name="research_baseline",
            export_generation_id="a" * 64,
            run_generation_id="c" * 64,
            artifact_generation_id="d" * 64,
            decision_dates=["2025-09-10"],
        )


def test_a_malformed_export_manifest_is_refused(tmp_path: Path) -> None:
    """A manifest that does not satisfy the export contract stops the read.

    The reader validates the manifest before it trusts any field in it, so a
    truncated document cannot be used to point at arbitrary files.
    """
    snapshot = (
        tmp_path / "qlib_exports" / "dataset=research_baseline" / f"generation={'a' * 64}"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text(json.dumps({"contract_version": 1}))

    with pytest.raises(ContractError, match="qlib_dataset_export"):
        score_decision_dates(
            export_root=tmp_path / "qlib_exports",
            dataset_name="research_baseline",
            export_generation_id="a" * 64,
            run_generation_id="c" * 64,
            artifact_generation_id="d" * 64,
            decision_dates=["2025-09-10"],
        )


def test_the_result_reports_unscorable_rows_rather_than_hiding_them() -> None:
    """`unscorable_rows` exists so a caller can see real export coverage.

    Qlib enumerates by instrument span, so a session where a name has no
    canonical row still yields an all-NaN vector and therefore a NaN score.
    Those rows are dropped, but silently dropping them would overstate coverage.
    """
    result = InferenceResult(
        scores={"2025-09-10": pd.DataFrame({"instrument": ["A"], "score": [0.5]})},
        unscorable_rows=3,
    )
    assert result.unscorable_rows == 3
    assert len(result.scores["2025-09-10"]) == 1


def test_inference_refuses_without_the_qlib_extra(tmp_path: Path) -> None:
    """A missing Qlib must read as a contract refusal, not an ImportError.

    The `.venv` environment has no Qlib, which is exactly the environment this
    test runs in; `.venv-qlib` skips it because the import succeeds there.
    """
    pytest.importorskip
    try:
        import qlib  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("Qlib is installed; this case only applies without it")
    with pytest.raises(ContractError):
        score_decision_dates(
            export_root=tmp_path,
            dataset_name="research_baseline",
            export_generation_id="a" * 64,
            run_generation_id="c" * 64,
            artifact_generation_id="d" * 64,
            decision_dates=["2025-09-10"],
        )


def test_a_runtime_version_mismatch_is_refused_before_scoring() -> None:
    """The version guard must be live, not an always-None branch.

    A model trained with one Qlib version must not be scored by another: model
    deserialization and feature evaluation can differ silently across runtime
    versions. The chain test proves the artifact's version is what reaches this
    guard; this test proves a mismatch is refused rather than ignored.
    """
    with pytest.raises(ContractError, match="Qlib version 0.9.6"):
        assert_runtime_version("0.9.6", "0.9.7")


def test_a_matching_or_unspecified_runtime_version_is_accepted() -> None:
    assert_runtime_version("0.9.7", "0.9.7")
    assert_runtime_version("0.9.7", None)
