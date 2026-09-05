from __future__ import annotations

import dataclasses

from datetime import date, datetime, timezone
from pathlib import Path
import json

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore, UniverseSnapshotStore
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.canonical_v2 import file_sha256_bytes, CanonicalV2Store
from uq.contracts.schema import load_schema
from uq.contracts.gate_contracts import sha256_json
from uq.contracts.model_layer import bind_reviewed_quality_decision, create_reviewed_quality_decision, model_manifest_identities
from uq.models.dataset_writer import DatasetWriter
from uq.models.feature_preprocessing import FeaturePreprocessorBuilder
from uq.models.features import FeatureSchemaBuilder
from uq.research_chain import DatasetStageAdapter
from uq.research_chain.owning_contracts import AdjustedPriceDatasetStore, FeaturePreprocessingStore, FeatureSchemaStore, LabelStore
from uq.models.labels import LabelBuilder
from tests.review_key import REVIEWER_PRIVATE_KEY
from uq.errors import ContractError
from uq.factors.store import FactorStore
from uq.research_chain import FactorStageAdapter, FileResearchRunStore
from uq.research_chain.resolver import ResolvedExecutionPlan, ResolvedStageBinding

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "00000000-0000-4000-8000-000000000002"


def _publish_factor(root: Path) -> FactorStore:
    from tests.test_model_end_to_end import _publish_factor

    _publish_factor(root)
    return FactorStore(root, FactorRegistry(ROOT))


def _plan(manifest: dict) -> ResolvedExecutionPlan:
    request = {
        "request_content_generation_id": manifest["generation_id"],
        "manifest_digest_sha256": manifest["manifest_digest_sha256"],
        "run_id": RUN_ID,
    }
    binding = ResolvedStageBinding(
        stage="factor_computation",
        output_family="factor_partition",
        generation_id=manifest["generation_id"],
        manifest_digest_sha256=manifest["manifest_digest_sha256"],
        data_checksum_sha256=manifest["data_checksum_sha256"],
    )
    return ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256=manifest["manifest_digest_sha256"],
        stage_plan_sha256="0" * 64,
        stage_bindings=(binding,),
        resolved_execution_plan_sha256="0" * 64,
    )


def test_factor_stage_binds_verified_partition(tmp_path: Path) -> None:
    store = _publish_factor(tmp_path)
    run_store = FileResearchRunStore(tmp_path)
    from tests.test_model_end_to_end import _load_factor_document

    manifest_path = next((tmp_path / "factors").rglob("manifest.json"))
    manifest = _load_factor_document(tmp_path, __import__("json").loads(manifest_path.read_text())["generation_id"])
    adapter = FactorStageAdapter(store, run_store)
    result = adapter.run(
        _plan(manifest),
        runner_identity={
            "code_fingerprint": "0" * 64,
            "environment_profile": "locked-test",
            "lock_digest_sha256": "0" * 64,
        },
        quality_decision_checksum_sha256="a" * 64,
        created_at="2026-01-30T07:00:00+00:00",
    )

    assert result.published_state.manifest_path.is_file()
    state = run_store.read_state(
        manifest["generation_id"], RUN_ID, "factor_computation",
        result.published_state.manifest_digest_sha256,
    )
    binding = state["stage_records"][-1]["output_bindings"][0]
    assert binding["output_family"] == "factor_partition"
    assert binding["generation_id"] == manifest["generation_id"]
    assert binding["physical_path"].startswith("factors/")
    assert run_store.list_state_snapshots(manifest["generation_id"], RUN_ID)[-1].stage == "factor_computation"


def test_factor_stage_rejects_tampered_partition(tmp_path: Path) -> None:
    store = _publish_factor(tmp_path)
    manifest_path = next((tmp_path / "factors").rglob("manifest.json"))
    manifest = store.read_manifest(__import__("json").loads(manifest_path.read_text())["generation_id"])
    data_path = manifest_path.parent / "data.parquet"
    data_path.write_bytes(data_path.read_bytes() + b"tampered")

    adapter = FactorStageAdapter(store, FileResearchRunStore(tmp_path))
    with pytest.raises(ContractError, match="tampered factor data"):
        adapter.run(
            _plan(manifest),
            runner_identity={
                "code_fingerprint": "0" * 64,
                "environment_profile": "locked-test",
                "lock_digest_sha256": "0" * 64,
            },
            quality_decision_checksum_sha256="a" * 64,
        )


def test_factor_stage_rejects_binding_mismatch(tmp_path: Path) -> None:
    store = _publish_factor(tmp_path)
    manifest_path = next((tmp_path / "factors").rglob("manifest.json"))
    manifest = store.read_manifest(__import__("json").loads(manifest_path.read_text())["generation_id"])
    plan = _plan(manifest)
    assert plan.stage_bindings[0].manifest_digest_sha256 == manifest["manifest_digest_sha256"]
    assert plan.stage_bindings[0].data_checksum_sha256 == manifest["data_checksum_sha256"]


def _dataset_plan(factor_manifest: dict, universe_manifest: dict, adjusted_manifest: dict, label_manifest: dict, preprocessing: dict) -> ResolvedExecutionPlan:
    request = json.loads((ROOT / "evidence/research-chain/phase-0/fixtures/research_run_request-valid.json").read_text())
    request["factor_binding"].update({
        "generation_id": factor_manifest["generation_id"],
        "manifest_digest_sha256": factor_manifest["manifest_digest_sha256"],
    })
    request["universe_snapshot_binding"].update({
        "generation_id": universe_manifest["generation_id"],
        "manifest_digest_sha256": universe_manifest.get("manifest_digest_sha256", "0" * 64),
    })
    request["adjusted_price_binding"].update({
        "generation_id": adjusted_manifest["generation_id"],
        "data_checksum_sha256": adjusted_manifest["data_checksum_sha256"],
    })
    request["label_binding"].update({
        "generation_id": label_manifest["generation_id"],
        "manifest_digest_sha256": label_manifest["manifest_digest_sha256"],
    })
    request["feature_preprocessing_binding"].update({
        "generation_id": preprocessing["generation_id"],
        "manifest_digest_sha256": preprocessing["manifest_digest_sha256"],
    })
    request["window_start_date"] = "2026-01-05"
    request["window_end_date"] = "2026-01-30"
    request["request_content_generation_id"] = "0" * 64
    request["manifest_digest_sha256"] = "0" * 64
    from uq.contracts.model_layer import research_contract_identities
    request["request_content_generation_id"], request["manifest_digest_sha256"] = research_contract_identities(
        request, schema_name="research_run_request"
    )
    request = {
        "request_content_generation_id": request["request_content_generation_id"],
        "manifest_digest_sha256": request["manifest_digest_sha256"],
        "run_id": RUN_ID,
        "window_start_date": request["window_start_date"],
        "window_end_date": request["window_end_date"],
        "dataset_policy_template": request["dataset_policy_template"],
    }
    return ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
        stage_plan_sha256="0" * 64,
        stage_bindings=(
            ResolvedStageBinding(stage="factor_computation", output_family="factor_partition", generation_id=factor_manifest["generation_id"], manifest_digest_sha256=factor_manifest["manifest_digest_sha256"], data_checksum_sha256=factor_manifest["data_checksum_sha256"]),
            ResolvedStageBinding(stage="factor_computation", output_family="universe_snapshot", generation_id=universe_manifest["generation_id"], manifest_digest_sha256=universe_manifest.get("manifest_digest_sha256", "0" * 64), data_checksum_sha256=universe_manifest["members_artifact"]["checksum_sha256"]),
            ResolvedStageBinding(stage="dataset_preparation", output_family="adjusted_price_dataset", generation_id=adjusted_manifest["generation_id"], manifest_digest_sha256=adjusted_manifest["manifest_digest_sha256"], data_checksum_sha256=adjusted_manifest["data_checksum_sha256"]),
            ResolvedStageBinding(stage="dataset_preparation", output_family="label_set", generation_id=label_manifest["generation_id"], manifest_digest_sha256=label_manifest["manifest_digest_sha256"], data_checksum_sha256=label_manifest["data_checksum_sha256"]),
            ResolvedStageBinding(stage="dataset_preparation", output_family="feature_preprocessing", generation_id=preprocessing["generation_id"], manifest_digest_sha256=preprocessing["manifest_digest_sha256"], data_checksum_sha256=preprocessing["output_frame_sha256"]),
        ),
        resolved_execution_plan_sha256="0" * 64,
    )


def _publish_adjusted_prices(root: Path, dates: list[pd.Timestamp], instruments: list[str]) -> dict:
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    rows = []
    for instrument in instruments:
        for index, day in enumerate(dates):
            rows.append({
                "instrument": instrument, "datetime": day,
                "open": 10.0, "high": 11.0 + index, "low": 9.8, "close": 10.0 + index * 0.1,
                "volume": 10000.0, "amount": 100000.0,
            })
    frame = pd.DataFrame(rows)
    store = CanonicalV2Store(root)
    generation = store.prepare_generation(schema, dates[0].date(), frame, {}, {})
    report = {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [{"name": "coverage", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        "errors": [],
        "warnings": [],
    }
    report_directory = QualityReportStore().save(root, report)
    checksum = file_sha256_bytes((report_directory / "report.json").read_bytes())
    store.publish(schema, dates[0].date(), frame, {}, {}, quality_checksum=checksum)
    return AdjustedPriceDatasetStore(root).read_manifest(generation)


def _publish_universe(root: Path, instruments: list[str]) -> dict:
    members = pd.DataFrame({"instrument": instruments})
    document = {
        "universe_version": 1,
        "universe_id": "research-whitelist",
        "source": "test://research-whitelist",
        "snapshot_time": "2026-01-05T00:00:00Z",
        "visibility_time": "2026-01-05T00:00:00Z",
        "valid_from": "2026-01-05",
        "valid_to": None,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
        "membership_evidence": "deterministic research chain test fixture",
    }
    return json.loads((UniverseSnapshotStore(root).save(root, document, members) / "manifest.json").read_text())


def _preprocessing_quality_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="feature_preprocessing_v1",
        policy="cross_sectional_stateless_v1",
        status="passed",
        checks=[{"name": "key_reconciliation", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def _dataset_quality_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="model_dataset_v1", policy="reject_all", status="passed",
        checks=[{"name": "row_count", "threshold": 0, "observed": 60, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def _prepare_dataset_store(tmp_path: Path) -> tuple[DatasetStageAdapter, ResolvedExecutionPlan, FactorStore]:
    from tests.test_model_end_to_end import _publish_factor as publish_global_factor
    publish_global_factor(tmp_path)
    factor_store = FactorStore(tmp_path, FactorRegistry(ROOT))
    factor_manifest_path = next((tmp_path / "factors").rglob("manifest.json"))
    factor_manifest = factor_store.read_manifest(json.loads(factor_manifest_path.read_text())["generation_id"])

    dates = pd.bdate_range("2026-01-05", periods=20)
    instruments = [f"INST{index}" for index in range(3)]
    adjusted_manifest = _publish_adjusted_prices(tmp_path, dates, instruments)
    universe_manifest = _publish_universe(tmp_path, instruments)

    _, factor_frame = factor_store.read_partition(factor_manifest["generation_id"])
    builder = FeaturePreprocessorBuilder(
        preprocess_name="research-standardized-v1", semantic_version="1.0.0",
        transform="standardize_cross_section", min_group_observations=2,
    )
    preprocessed = builder.transform(factor_frame)
    input_schema = FeatureSchemaBuilder().build(factor_frame, source_factor_set="basic", source_factor_version="1.0.0")
    output_schema = FeatureSchemaBuilder().build(preprocessed, source_factor_set="basic", source_factor_version="1.0.0")
    for column in output_schema["columns"]:
        column["transform_status"] = "standardized"
        column["source_factor"] = column["name"]
    from uq.contracts.model_layer import model_manifest_identities
    output_schema["generation_id"], output_schema["manifest_digest_sha256"] = model_manifest_identities(
        output_schema, schema_name="feature_schema"
    )
    preprocessing = builder.build(
        factor_frame, preprocessed,
        input_factor_set=factor_manifest["factor_set"],
        input_factor_version=factor_manifest["factor_version"],
        input_factor_generation_ids=[factor_manifest["generation_id"]],
        ordered_features=["volume_ratio_20d"],
        input_feature_schema=input_schema,
        output_feature_schema=output_schema,
    )
    feature_store_root = tmp_path / "governed"
    for document in (input_schema, output_schema):
        directory = feature_store_root / "feature_schemas" / f"generation={document['generation_id']}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(json.dumps(document))
    preprocessing_decision = create_reviewed_quality_decision(
        binding_type="feature_preprocessing_v1",
        policy="cross_sectional_stateless_v1",
        status="passed",
        checks=[{"name": "key_reconciliation", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    bound_preprocessing, _ = bind_reviewed_quality_decision(
        preprocessing_decision,
        binding_type="feature_preprocessing_v1",
        subject_generation_id=preprocessing["generation_id"],
        subject_content_sha256=preprocessing["generation_id"],
    )
    preprocessing = {**preprocessing, "quality_report_checksum_sha256": bound_preprocessing["report_checksum_sha256"]}
    from uq.contracts.model_layer import model_manifest_identities
    preprocessing["generation_id"], preprocessing["manifest_digest_sha256"] = model_manifest_identities(
        preprocessing, schema_name="feature_preprocessing", exclude_fields={"quality_report_checksum_sha256"}
    )
    preprocessing_directory = feature_store_root / "feature_preprocessing" / f"generation={preprocessing['generation_id']}"
    preprocessing_directory.mkdir(parents=True)
    (preprocessing_directory / "manifest.json").write_text(json.dumps(preprocessing))

    label_frame = factor_frame.copy()
    label_frame["close"] = 10.0
    label_frame["adj_factor"] = 1.0
    label_frame["suspended"] = False
    label_frame["listing_date"] = pd.Timestamp("2020-01-01", tz="UTC")
    label_frame["limit_up"] = False
    label_frame["limit_down"] = False
    label_frame["delisted"] = False
    label_checksum = sha256_json({"rows": [
        [str(row[0]), pd.Timestamp(row[1]).isoformat(), float(row[2]), float(row[3]), bool(row[4]), str(pd.Timestamp(row[5]).date())]
        for row in label_frame.sort_values(["instrument", "datetime"], kind="mergesort")[["instrument", "datetime", "close", "adj_factor", "suspended", "listing_date"]].itertuples(index=False)
    ]})
    binding = {
        "binding": "adjusted_price", "dataset": "bars_adjusted", "schema_version": "adjusted-v1",
        "partition_date": "2026-01-30", "generation_id": adjusted_manifest["generation_id"],
        "data_checksum_sha256": label_checksum, "visible_cutoff": "2026-01-30T15:00:00+08:00",
    }
    label_manifest = LabelBuilder(name="return_5d", semantic_version="1.0.0").build(label_frame, upstream_bindings=[binding])
    label_directory = tmp_path / "label_sets" / f"generation={label_manifest['generation_id']}"
    label_directory.mkdir(parents=True)
    label_frame[["instrument", "datetime"]].rename(columns={"datetime": "decision_date"}).assign(label=0.01).to_parquet(label_directory / "data.parquet", index=False)
    (label_directory / "manifest.json").write_text(json.dumps(label_manifest))

    plan = _dataset_plan(factor_manifest, universe_manifest, adjusted_manifest, label_manifest, preprocessing)
    adapter = DatasetStageAdapter(
        factor_store=factor_store,
        adjusted_price_store=AdjustedPriceDatasetStore(tmp_path),
        label_store=LabelStore(tmp_path),
        universe_store=UniverseSnapshotStore(tmp_path),
        feature_schema_store=FeatureSchemaStore(feature_store_root),
        preprocessing_store=FeaturePreprocessingStore(feature_store_root),
        dataset_writer=DatasetWriter(tmp_path),
        run_store=FileResearchRunStore(tmp_path),
    )
    return adapter, plan, factor_store


def test_dataset_stage_binds_reviewed_policy_and_verified_inputs(tmp_path: Path) -> None:
    adapter, plan, _ = _prepare_dataset_store(tmp_path)
    result = adapter.run(
        plan,
        runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "0" * 64},
        quality_decision=_dataset_quality_decision(),
        preprocessing_quality_decision=_preprocessing_quality_decision(),
        created_at="2026-01-30T07:00:00+00:00",
    )
    manifest = result.manifest
    assert manifest["dataset_name"] == "research_baseline"
    assert manifest["feature_preprocessing_generation_id"]
    state = FileResearchRunStore(tmp_path).read_state(
        plan.request["request_content_generation_id"], RUN_ID, "dataset_preparation",
        result.published_state.manifest_digest_sha256,
    )
    binding = state["stage_records"][-1]["output_bindings"][0]
    assert binding["output_family"] == "model_dataset"
    assert binding["generation_id"] == manifest["generation_id"]
    assert binding["physical_path"].startswith("datasets/")


def test_dataset_stage_rejects_wrong_reviewed_quality_decision(tmp_path: Path) -> None:
    adapter, plan, _ = _prepare_dataset_store(tmp_path)
    wrong_decision = _dataset_quality_decision()
    wrong_decision["binding_type"] = "prediction_set_v1"
    with pytest.raises(ContractError, match="does not match model_dataset_v1"):
        adapter.run(
            plan,
            runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "0" * 64},
            quality_decision=wrong_decision,
            preprocessing_quality_decision=_preprocessing_quality_decision(),
        )


def test_dataset_stage_rejects_adjusted_price_manifest_mismatch(tmp_path: Path) -> None:
    adapter, plan, _ = _prepare_dataset_store(tmp_path)
    binding = plan.stage_bindings[2]
    plan = dataclasses.replace(plan, stage_bindings=(
        *plan.stage_bindings[:2],
        ResolvedStageBinding(
            stage=binding.stage,
            output_family=binding.output_family,
            generation_id=binding.generation_id,
            manifest_digest_sha256="f" * 64,
            data_checksum_sha256=binding.data_checksum_sha256,
        ),
        *plan.stage_bindings[3:],
    ))
    with pytest.raises(ContractError, match="adjusted price binding manifest digest mismatch"):
        adapter.run(
            plan,
            runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "0" * 64},
            quality_decision=_dataset_quality_decision(),
            preprocessing_quality_decision=_preprocessing_quality_decision(),
        )


def test_dataset_stage_rejects_label_manifest_mismatch(tmp_path: Path) -> None:
    adapter, plan, _ = _prepare_dataset_store(tmp_path)
    binding = plan.stage_bindings[3]
    plan = dataclasses.replace(plan, stage_bindings=(
        *plan.stage_bindings[:3],
        ResolvedStageBinding(
            stage=binding.stage,
            output_family=binding.output_family,
            generation_id=binding.generation_id,
            manifest_digest_sha256="f" * 64,
            data_checksum_sha256=binding.data_checksum_sha256,
        ),
        *plan.stage_bindings[4:],
    ))
    with pytest.raises(ContractError, match="label binding manifest digest mismatch"):
        adapter.run(
            plan,
            runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "0" * 64},
            quality_decision=_dataset_quality_decision(),
            preprocessing_quality_decision=_preprocessing_quality_decision(),
        )


def test_dataset_stage_rejects_tampered_universe_membership(tmp_path: Path) -> None:
    adapter, plan, _ = _prepare_dataset_store(tmp_path)
    universe_path = tmp_path / "universes" / "research-whitelist" / plan.stage_bindings[1].generation_id / "members.csv"
    universe_path.write_text(universe_path.read_text() + "EXTRA\n")
    with pytest.raises(ContractError, match="tampered universe membership"):
        adapter.run(
            plan,
            runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "0" * 64},
            quality_decision=_dataset_quality_decision(),
            preprocessing_quality_decision=_preprocessing_quality_decision(),
        )


def test_dataset_stage_rejects_missing_upstream_binding(tmp_path: Path) -> None:
    adapter, plan, _ = _prepare_dataset_store(tmp_path)
    bindings = list(plan.stage_bindings)
    bindings[2] = ResolvedStageBinding(
        stage=bindings[2].stage,
        output_family=bindings[2].output_family,
        generation_id="0" * 64,
        manifest_digest_sha256="0" * 64,
        data_checksum_sha256="0" * 64,
    )
    plan = dataclasses.replace(plan, stage_bindings=tuple(bindings))
    with pytest.raises(ContractError, match="unpublished adjusted price dataset"):
        adapter.run(
            plan,
            runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "0" * 64},
            quality_decision=_dataset_quality_decision(),
            preprocessing_quality_decision=_preprocessing_quality_decision(),
        )


def test_dataset_stage_rejects_preprocessing_input_mismatch(tmp_path: Path) -> None:
    adapter, plan, factor_store = _prepare_dataset_store(tmp_path)
    binding = plan.stage_bindings[0]
    _, factor_frame = factor_store.read_partition(binding.generation_id)
    factor_frame.loc[0, "volume_ratio_20d"] += 1.0
    original_read = factor_store.read_partition

    def read_shifted_partition(generation_id: str):
        frame = original_read(generation_id)[1].copy()
        frame.loc[0, "volume_ratio_20d"] += 1.0
        return original_read(generation_id)[0], frame

    factor_store.read_partition = read_shifted_partition
    with pytest.raises(ContractError, match="preprocessing input frame fingerprint mismatch"):
        adapter.run(
            plan,
            runner_identity={"code_fingerprint": "0" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "0" * 64},
            quality_decision=_dataset_quality_decision(),
            preprocessing_quality_decision=_preprocessing_quality_decision(),
        )
