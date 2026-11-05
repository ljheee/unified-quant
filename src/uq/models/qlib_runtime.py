from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import joblib
import numpy as np
import pandas as pd
import qlib
from qlib.contrib.model.linear import LinearModel
from qlib.data import D
from qlib.data.dataset import DatasetH
from qlib.data.dataset.handler import DataHandlerLP
from qlib.data.dataset.loader import QlibDataLoader, StaticDataLoader

from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities
from ..errors import ContractError
from .dataset import resolve_train_split


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def _timed_step(
    name: str,
    callback: Callable[[str, float, str], None] | None,
) -> Iterator[None]:
    started = time.perf_counter()
    status = "passed"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        if callback is not None:
            callback(name, time.perf_counter() - started, status)


def build_static_qlib_frame(
    dataset_frame: pd.DataFrame,
    *,
    dataset_manifest: dict[str, Any],
    feature_columns: list[str],
    label_column: str,
    feature_mapping: dict[str, str],
    start_time: str,
    end_time: str,
    selected_dates: list[str] | None = None,
) -> pd.DataFrame:
    """Project an owning-store-verified dataset frame into Qlib's in-memory shape.

    Qlib's ``.bin`` files contain float32 values. Construct the static frame from
    one explicit float32 array so its values, row keys, column order and dtypes
    match ``QlibDataLoader`` without one open per instrument/field file.
    """
    expected_columns = ["instrument", "datetime", *feature_columns, label_column]
    if list(dataset_frame.columns) != expected_columns:
        raise ContractError("verified dataset frame columns do not match Qlib training schema")
    if len(dataset_frame) != dataset_manifest.get("row_count"):
        raise ContractError("verified dataset frame row count does not match its manifest")

    dates = pd.to_datetime(dataset_frame["datetime"])
    date_mask = (dates >= pd.Timestamp(start_time)) & (dates <= pd.Timestamp(end_time))
    if selected_dates is not None:
        date_mask &= dates.isin(pd.to_datetime(selected_dates))
    selected = dataset_frame.loc[date_mask, ["datetime", "instrument", *feature_columns, label_column]]
    if selected.empty:
        raise ContractError("Qlib selection contains no dataset rows")

    index = pd.MultiIndex.from_arrays(
        [
            pd.to_datetime(selected["datetime"]).to_numpy(copy=False),
            selected["instrument"].astype(str).str.lower().to_numpy(),
        ],
        names=["datetime", "instrument"],
    )
    values = selected[[*feature_columns, label_column]].to_numpy(
        dtype=np.float32, copy=True,
    )
    qlib_columns = pd.MultiIndex.from_tuples(
        [
            ("feature", f"${feature_mapping[column]}")
            for column in feature_columns
        ] + [("label", f"${feature_mapping[label_column]}")]
    )
    result = pd.DataFrame(values, index=index, columns=qlib_columns, copy=False)
    result.sort_index(inplace=True)
    return result


class QlibRuntimeTrainer:
    """Train through the Qlib model API against a verified governed export."""

    def __init__(self, *, kernels: int = 1) -> None:
        if isinstance(kernels, bool) or not isinstance(kernels, int) or kernels < 1:
            raise ContractError("Qlib loader kernels must be a positive integer")
        self.kernels = kernels

    def train(
        self,
        *,
        definition: dict[str, Any],
        dataset_manifest: dict[str, Any],
        export_manifest: dict[str, Any],
        receipt_manifest: dict[str, Any],
        verified_export: tuple[dict[str, Any], Path],
        verified_dataset: tuple[dict[str, Any], pd.DataFrame] | None = None,
        feature_columns: list[str],
        label_column: str,
        timing_callback: Callable[[str, float, str], None] | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        algorithm = definition["algorithm"]
        if algorithm not in {"qlib_linear", "qlib_lightgbm"}:
            raise ContractError(f"Qlib trainer does not support {algorithm}")
        if algorithm == "qlib_lightgbm" and definition.get("contract_version") != 2:
            raise ContractError("qlib_lightgbm requires model_definition.v2")
        if definition["serializer_version"] != "joblib-v1":
            raise ContractError("Qlib trainer requires serializer joblib-v1")

        verified_manifest, snapshot = verified_export
        for family, document in (
            ("model_definition", definition),
            ("model_dataset", dataset_manifest),
            ("qlib_dataset_export", export_manifest),
            ("qlib_init_receipt", receipt_manifest),
        ):
            ModelContractLoader.validate(family, document)

        ModelContractLoader.validate("qlib_dataset_export", verified_manifest)
        if (
            verified_manifest["generation_id"] != export_manifest["generation_id"]
            or verified_manifest["manifest_digest_sha256"] != export_manifest["manifest_digest_sha256"]
        ):
            raise ContractError("verified Qlib export identity mismatch")
        if receipt_manifest["export_generation_id"] != export_manifest["generation_id"]:
            raise ContractError("Qlib receipt does not bind the verified export")
        if receipt_manifest["export_manifest_digest_sha256"] != export_manifest["manifest_digest_sha256"]:
            raise ContractError("Qlib receipt export digest mismatch")
        file_list_checksum = _sha256_text(json.dumps([entry["path"] for entry in export_manifest["files"]]))
        if receipt_manifest["file_list_checksum_sha256"] != file_list_checksum:
            raise ContractError("Qlib receipt file-list mismatch")
        for field in ("calendar_checksum_sha256", "instruments_checksum_sha256", "feature_mapping_checksum_sha256"):
            if receipt_manifest[field] != export_manifest[field]:
                raise ContractError(f"Qlib receipt {field.removesuffix('_checksum_sha256')} mismatch")
        if receipt_manifest["qlib_import_path"] != "qlib" or receipt_manifest["qlib_version"] != qlib.__version__:
            raise ContractError("Qlib receipt runtime identity mismatch")
        if receipt_manifest["no_ungoverned_source_assertion"] is not True:
            raise ContractError("Qlib receipt does not assert a governed source boundary")
        verified_dataset_frame: pd.DataFrame | None = None
        if verified_dataset is not None:
            verified_dataset_manifest, verified_dataset_frame = verified_dataset
            if verified_dataset_manifest != dataset_manifest:
                raise ContractError("verified dataset frame manifest does not match training input")
        with _timed_step("model_training/export_file_verification", timing_callback):
            for entry in export_manifest["files"]:
                path = snapshot / entry["path"]
                if (
                    not path.is_file()
                    or path.stat().st_size != entry["byte_size"]
                    or file_sha256_bytes(path.read_bytes()) != entry["checksum_sha256"]
                ):
                    raise ContractError(f"tampered Qlib export file: {entry['path']}")

        mapping_path = snapshot / "feature_mapping.json"
        feature_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        expected_columns = set(feature_columns) | {label_column}
        if set(feature_mapping) != expected_columns:
            raise ContractError("Qlib export feature mapping does not match training schema")
        if len(feature_columns) != len(set(feature_columns)):
            raise ContractError("Qlib training feature columns must be unique")
        if label_column in feature_columns:
            raise ContractError("Qlib label column cannot also be a feature column")

        ordered_features = dataset_manifest["ordered_features"]
        if feature_columns != ordered_features:
            raise ContractError("Qlib training features do not match dataset ordered features")

        calendar_dates = (snapshot / "calendars" / "day.txt").read_text(encoding="utf-8").splitlines()
        calendar_dates = [date for date in calendar_dates if date]
        if not calendar_dates:
            raise ContractError("Qlib export calendar is empty")
        # DataHandlerLP eagerly loads its configured range. Resolve the governed
        # train interval before constructing it so validation/test rows are not
        # read merely to be discarded by DatasetH.prepare().
        train_start, train_end = resolve_train_split(dataset_manifest, calendar_dates)

        with _timed_step("model_training/qlib_initialization", timing_callback):
            qlib.init(
                provider_uri=str(snapshot),
                region="cn",
                expression_cache=None,
                dataset_cache=None,
                kernels=self.kernels,
                logging_level="WARNING",
            )
            feature_fields = [f"${feature_mapping[column]}" for column in feature_columns]
            label_field = f"${feature_mapping[label_column]}"
        if verified_dataset_frame is not None:
            with _timed_step("model_training/static_frame_conversion", timing_callback):
                qlib_frame = build_static_qlib_frame(
                    verified_dataset_frame,
                    dataset_manifest=dataset_manifest,
                    feature_columns=feature_columns,
                    label_column=label_column,
                    feature_mapping=feature_mapping,
                    start_time=train_start,
                    end_time=train_end,
                )
                data_loader = StaticDataLoader(qlib_frame)
            handler_instruments = None
        else:
            instruments = sorted(
                line.split("\t", 1)[0].lower()
                for line in (snapshot / "instruments" / "all.txt").read_text(encoding="utf-8").splitlines()
                if line
            )
            if not instruments:
                raise ContractError("Qlib export instruments are empty")
            data_loader = QlibDataLoader(
                config={"feature": feature_fields, "label": [label_field]},
                freq="day",
            )
            handler_instruments = instruments
        with _timed_step("model_training/qlib_data_loading", timing_callback):
            handler = DataHandlerLP(
                instruments=handler_instruments,
                start_time=train_start,
                end_time=train_end,
                data_loader=data_loader,
                infer_processors=[],
                learn_processors=[],
                process_type="append",
            )
        # cr.md C2: fit strictly on the dataset manifest's declared train split.
        # The export calendar spans train+validation+test with labels attached,
        # so deriving the segment from the calendar leaks every evaluation.
        # Purge/embargo separation between splits is enforced at dataset build
        # time by SplitValidator; here we must simply not fit beyond train.
        with _timed_step("model_training/train_split_prepare", timing_callback):
            dataset = DatasetH(handler, segments={"train": (train_start, train_end)})
            prepared = dataset.prepare("train", col_set=["feature", "label"])
            prepared = prepared.dropna()
            if prepared.empty or len(prepared) < 2:
                raise ContractError("Qlib training requires at least two supervised observations")

        with _timed_step("model_training/model_fit", timing_callback):
            np.random.seed(int(definition["seed_policy"]["base_seed"]))
            if algorithm == "qlib_linear":
                alpha = float(definition["hyperparameters"].get("alpha", 1.0))
                fit_intercept = bool(definition["hyperparameters"].get("fit_intercept", False))
                model = LinearModel(estimator="ridge", alpha=alpha, fit_intercept=fit_intercept)
                model.fit(dataset)
                runtime_name = "qlib_linear"
                runtime_import_path = "qlib.contrib.model.linear.LinearModel"
            else:
                try:
                    import lightgbm
                    from lightgbm import LGBMRegressor
                except ImportError as exc:
                    raise ContractError("qlib_lightgbm training requires the gbdt extra") from exc
                allowed_parameters = {
                    "n_estimators", "num_leaves", "learning_rate", "min_child_samples",
                    "max_depth", "reg_alpha", "reg_lambda", "subsample", "colsample_bytree",
                }
                parameters = dict(definition["hyperparameters"])
                unknown = sorted(set(parameters) - allowed_parameters)
                if unknown:
                    raise ContractError(f"unsupported qlib_lightgbm hyperparameters: {unknown}")
                features = prepared.xs("feature", axis=1, level=0, drop_level=True)
                labels = prepared.xs("label", axis=1, level=0, drop_level=True)
                if labels.shape[1] != 1:
                    raise ContractError("qlib_lightgbm requires exactly one label column")
                feature_values = features.to_numpy(dtype="float64")
                label_values = labels.iloc[:, 0].to_numpy(dtype="float64")
                if not np.isfinite(feature_values).all() or not np.isfinite(label_values).all():
                    raise ContractError("qlib_lightgbm train data contains non-finite values")
                model = LGBMRegressor(
                    objective="regression",
                    random_state=int(definition["seed_policy"]["base_seed"]),
                    n_jobs=1,
                    deterministic=True,
                    force_col_wise=True,
                    verbosity=-1,
                    **parameters,
                )
                model.fit(features, label_values)
                runtime_name = f"qlib_lightgbm:{lightgbm.__version__}"
                runtime_import_path = "lightgbm.sklearn.LGBMRegressor"

        with _timed_step("model_training/artifact_serialization", timing_callback):
            artifact_buffer = io.BytesIO()
            joblib.dump(model, artifact_buffer, compress=3)
            artifact_bytes = artifact_buffer.getvalue()
            artifact_checksum = file_sha256_bytes(artifact_bytes)
        run_content_generation_id = definition["model_run_content_generation_id"]
        if not isinstance(run_content_generation_id, str) or len(run_content_generation_id) != 64:
            raise ContractError("model definition is not bound to a validated model_run_content_generation_id")

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "artifact_filename": "model.joblib",
            "artifact_checksum_sha256": artifact_checksum,
            "byte_size": len(artifact_bytes),
            "runtime_name": runtime_name,
            "runtime_version": qlib.__version__,
            "runtime_import_path": runtime_import_path,
            "model_run_content_generation_id": run_content_generation_id,
            "serialization_profile_id": "joblib-v1",
            "run_id": "00000000-0000-4000-8000-000000000000",
            "created_at": "1970-01-01T00:00:00+00:00",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        return manifest, artifact_bytes
