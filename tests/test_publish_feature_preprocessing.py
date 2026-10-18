"""Regression tests for scripts/publish_feature_preprocessing.py.

This entry point is the last missing producer for a v5 execute: it turns governed
factor partitions into a reviewed `feature_preprocessing` artifact. The cases that
matter are the ones where a wrong choice would publish something its only consumer
must reject:

* the artifact pins `input_frame_sha256`, so the frame it describes has to be the
  frame the research stage reconstructs -- which under `drop_incomplete_rows` is the
  post-drop frame;
* the entry point must never sign its own review, because a self-attested quality
  decision is not a review;
* a null feature without the drop flag must be refused rather than published.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.model_layer import create_reviewed_quality_decision
from uq.errors import ContractError
from uq.factors.engine import FactorEngine
from uq.factors.store import FactorStore, factor_generation
from uq.research_chain.owning_contracts import (
    FeaturePreprocessingStore,
    FeatureSchemaStore,
)
from tests.review_key import REVIEWER_PRIVATE_KEY

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "publish_feature_preprocessing", _ROOT / "scripts" / "publish_feature_preprocessing.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

DATES = pd.bdate_range("2026-01-05", periods=4)
_CUTOFF = datetime.fromisoformat("2026-01-08T15:00:00+08:00")
INSTRUMENTS = ("600000.XSHG", "000001.XSHE", "300750.XSHE")


def _publish_factor(root: Path, frame: pd.DataFrame, partition_date: date) -> dict:
    """Publish one factor partition with a quality report the store will accept.

    The report has to be the one the governed engine would produce for this frame,
    not a hand-written stub: `FactorStore.publish` recomputes the local quality
    verdict and refuses a report that does not agree with it. A null in a factor
    column is legal here (the reviewed check is a null-*rate* threshold), which is
    exactly the production situation this entry point has to handle.
    """
    registry = FactorRegistry(_ROOT)
    engine = FactorEngine(root, registry, run_visible_cutoff=_CUTOFF)
    request = engine.build_request(
        trade_date=partition_date, factor_set="basic", factor_version="2.0.0",
    )
    result = engine.create_result(
        request, frame, input_lineage=({
            "factor_set": "basic", "factor_version": "2.0.0",
            "dataset": "bars_daily", "schema_version": "v2",
            "generation_id": "a" * 64, "data_checksum_sha256": "b" * 64,
        },),
    )
    if result.status != "passed":
        raise AssertionError(f"engine refused the test frame: {result.errors}")
    arguments = {
        "frame": frame,
        "factor_set": "basic",
        "factor_version": "2.0.0",
        "partition_date": partition_date,
        "input_dataset": "bars_daily",
        "input_schema_version": "v2",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime.fromisoformat("2026-01-04T15:00:00+08:00"),
    }
    # `factor_generation` otherwise rebuilds a registry from this module's location
    # and resolves `basic/1.0.0`, which produces a manifest the store then refuses on
    # fingerprint grounds. Pass the injected registry so generation and publish agree.
    generation = factor_generation(**arguments, registry=registry)
    local = result.quality_report
    QualityReportStore().save(root, {
        "report_version": 1, "binding_type": "factor_v1",
        "bound_generation_id": generation, "policy": local["policy"],
        "status": local["status"], "checks": local["checks"],
        "errors": local["errors"], "warnings": local["warnings"],
    })
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    FactorStore(root, registry).publish(
        **{k: v for k, v in arguments.items() if k != "frame"}, frame=frame
    )
    return json.loads(
        (root / "factors").rglob("manifest.json").__next__().read_text()
    )


def _factor_frame(null_cell: bool = False) -> pd.DataFrame:
    rows = []
    for index, instrument in enumerate(INSTRUMENTS):
        for position, day in enumerate(DATES):
            value = float(index + position)
            if null_cell and instrument == "600000.XSHG" and position == 1:
                value = float("nan")
            rows.append({
                "instrument": instrument, "datetime": day,
                "range_ratio_1d": value, "close_location_1d": value,
                "amount_20d": value, "volume_ratio_20d": value,
            })
    # `logical_fingerprint` is row-order sensitive, and `FactorStore.publish`
    # re-derives it from the frame it reads back (which parquet returns in the
    # written order). Publish a canonically sorted frame so the two agree.
    return pd.DataFrame(rows).sort_values(
        ["instrument", "datetime"], kind="mergesort"
    ).reset_index(drop=True)


def _decision_document(tmp_path: Path, subject_generation_id: str) -> Path:
    """A decision signed by the *external* reviewer role, as a real run would carry."""
    decision = create_reviewed_quality_decision(
        binding_type="feature_preprocessing_v1",
        policy="cross_sectional_stateless_v1",
        status="passed",
        checks=[{"name": "key_reconciliation", "threshold": 0, "observed": 0,
                 "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
        subject_generation_id=subject_generation_id,
    )
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps({"quality_decisions": {"feature_preprocessing": decision}}))
    return path


def _argv(tmp_path: Path, *, drop: bool, decisions: Path, partition: date) -> list[str]:
    return [
        "--factor-root", str(tmp_path / "factors-root"),
        "--feature-schema-root", str(tmp_path / "governed"),
        "--preprocessing-root", str(tmp_path / "governed"),
        "--factor-set", "basic", "--factor-version", "2.0.0",
        "--preprocess-name", "basic-standardized-v1",
        "--semantic-version", "1.0.0",
        "--transform", "standardize_cross_section",
        "--partition-date", partition.isoformat(),
        "--quality-decisions-json", str(decisions),
        "--out", str(tmp_path / "report.json"),
        *(["--drop-incomplete-rows"] if drop else []),
    ]


def _prepare(tmp_path: Path, null_cell: bool = False) -> date:
    root = tmp_path / "factors-root"
    root.mkdir(parents=True, exist_ok=True)
    partition = DATES[0].date()
    _publish_factor(root, _factor_frame(null_cell), partition)
    return partition


def _expected_generation(tmp_path: Path, drop: bool, null_cell: bool) -> str:
    """The generation the artifact will carry, derived the same way the script does."""
    from uq.models.feature_preprocessing import FeaturePreprocessorBuilder, complete_rows
    from uq.models.features import FeatureSchemaBuilder
    from uq.contracts.model_layer import model_manifest_identities

    frame = _factor_frame(null_cell)
    features = [c for c in frame.columns if c not in ("instrument", "datetime")]
    if drop and null_cell:
        frame = complete_rows(frame, features)
    builder = FeaturePreprocessorBuilder(
        preprocess_name="basic-standardized-v1", semantic_version="1.0.0",
        transform="standardize_cross_section",
    )
    transformed = builder.transform(frame, features)
    input_schema = FeatureSchemaBuilder().build(
        frame, source_factor_set="basic", source_factor_version="2.0.0"
    )
    output_schema = FeatureSchemaBuilder().build(
        transformed, source_factor_set="basic", source_factor_version="2.0.0"
    )
    for column in output_schema["columns"]:
        column["transform_status"] = "standardized"
        column["source_factor"] = column["name"]
    output_schema["generation_id"], output_schema["manifest_digest_sha256"] = (
        model_manifest_identities(output_schema, schema_name="feature_schema")
    )
    manifest = builder.build(
        frame, transformed,
        input_factor_set="basic", input_factor_version="2.0.0",
        input_factor_generation_ids=[
            json.loads((tmp_path / "factors-root" / "factors").rglob("manifest.json").__next__().read_text())["generation_id"]
        ],
        ordered_features=features,
        input_feature_schema=input_schema, output_feature_schema=output_schema,
    )
    return manifest["generation_id"]


def test_publishes_an_artifact_the_governed_store_can_read_back(tmp_path: Path) -> None:
    partition = _prepare(tmp_path)
    decisions = _decision_document(tmp_path, _expected_generation(tmp_path, False, False))
    code = module.main(_argv(tmp_path, drop=False, decisions=decisions, partition=partition))
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text())
    store = FeaturePreprocessingStore(tmp_path / "governed")
    manifest = store.read_manifest(report["generation_id"])
    assert manifest["generation_id"] == report["generation_id"]
    assert manifest["ordered_features"] == [
        "range_ratio_1d", "close_location_1d", "amount_20d", "volume_ratio_20d"
    ]
    assert report["binding"]["family"] == "feature_preprocessing"


def test_the_artifact_never_signs_its_own_review(tmp_path: Path) -> None:
    partition = _prepare(tmp_path)
    decisions = _decision_document(tmp_path, _expected_generation(tmp_path, False, False))
    module.main(_argv(tmp_path, drop=False, decisions=decisions, partition=partition))
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["review"]["signed_by_script"] is False
    assert report["review"]["status"] == "passed"


def test_drop_incomplete_rows_publishes_the_post_drop_frame(tmp_path: Path) -> None:
    # The stage re-checks `input_frame_sha256` against the frame it reconstructs,
    # and under `drop_incomplete_rows` it reconstructs the post-drop frame. An
    # artifact built over the unfiltered frame would be refused at execute time, so
    # the publisher has to drop first -- and say what it dropped.
    partition = _prepare(tmp_path, null_cell=True)
    decisions = _decision_document(tmp_path, _expected_generation(tmp_path, True, True))
    code = module.main(_argv(tmp_path, drop=True, decisions=decisions, partition=partition))
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["dropped_incomplete_rows"]["rows"] == 1
    assert report["dropped_incomplete_rows"]["per_feature"] == {
        "range_ratio_1d": 1, "close_location_1d": 1,
        "amount_20d": 1, "volume_ratio_20d": 1,
    }
    assert report["rows"] == len(DATES) * len(INSTRUMENTS) - 1
    assert report["instruments"] == len(INSTRUMENTS)
    # the published artifact really attests the post-drop frame
    store = FeaturePreprocessingStore(tmp_path / "governed")
    manifest = store.read_manifest(report["generation_id"])
    assert manifest["output_frame_row_count"] == len(DATES) * len(INSTRUMENTS) - 1


def test_a_null_feature_without_the_drop_flag_is_refused(tmp_path: Path) -> None:
    # Without the flag the transform preserves nulls, and the dataset stage cannot
    # consume them under fail_closed. Publishing would create an artifact whose only
    # consumer must reject it.
    partition = _prepare(tmp_path, null_cell=True)
    decisions = _decision_document(tmp_path, "0" * 64)
    code = module.main(_argv(tmp_path, drop=False, decisions=decisions, partition=partition))
    assert code == 2
    assert not (tmp_path / "report.json").exists()


def test_a_missing_partition_is_a_configuration_failure(tmp_path: Path) -> None:
    _prepare(tmp_path)
    decisions = _decision_document(tmp_path, "0" * 64)
    code = module.main(
        _argv(tmp_path, drop=False, decisions=decisions, partition=date(2030, 1, 7))
    )
    assert code == 2


def test_requires_a_partition_selection(tmp_path: Path) -> None:
    _prepare(tmp_path)
    decisions = _decision_document(tmp_path, "0" * 64)
    argv = _argv(tmp_path, drop=False, decisions=decisions, partition=DATES[0].date())
    index = argv.index("--partition-date")
    del argv[index:index + 2]
    assert module.main(argv) == 2


def test_refuses_a_decisions_document_without_the_family(tmp_path: Path) -> None:
    partition = _prepare(tmp_path)
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"quality_decisions": {}}))
    code = module.main(_argv(tmp_path, drop=False, decisions=path, partition=partition))
    assert code == 2
