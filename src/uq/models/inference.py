"""Score a decision date by loading a governed model artifact.

Until now the research chain's prediction stage took its scores from an external
`--scores-json`. That made the chain's *plumbing* testable but left the model out
of the loop: the published artifact was never loaded, so any number that reached
portfolio construction came from outside the governed surface.

This module closes that gap. It reads the three artifacts that already exist --
the Qlib export, the init receipt, and the model artifact -- verifies each one's
identity against what the dataset stage declared, and runs the model over the
requested decision dates.

Deliberate boundaries:

* It never trains, never writes, and never signs a quality decision. It is a
  reader plus an inference call.
* It scores only dates the caller names. It does not decide which dates are
  decision dates; that is the request's business (`--scores-json` used to carry
  it, and `--score-dates` now does).
* It refuses to score a date the export's calendar does not contain. A date
  outside the calendar means the export was not built for this request, and
  inventing a row would be worse than failing.
* Qlib's provider lowercases instrument ids on export (see
  `qlib_export.py:105`). This module maps back to the canonical uppercase ids so
  the published prediction set joins against the dataset and the universe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import joblib
import numpy as np
import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.model_layer import ModelContractLoader
from ..errors import ContractError
from .trainer import ArtifactStore


@dataclass(frozen=True)
class InferenceResult:
    """Scores per decision date, plus how many rows Qlib could not score.

    `unscorable_rows` is reported rather than hidden: Qlib enumerates by the
    instrument span, so a session where a name has no canonical row still yields
    an all-NaN feature vector. Those rows are absent from the training frame too,
    so they are dropped -- but a caller that wants to know how much coverage the
    export really has should be able to see the count.
    """

    scores: dict[str, pd.DataFrame]
    unscorable_rows: int


def assert_runtime_version(
    actual_qlib_version: str,
    expected_qlib_version: str | None,
) -> None:
    """Refuse scoring when the installed Qlib differs from the trained runtime."""
    if expected_qlib_version is not None and actual_qlib_version != expected_qlib_version:
        raise ContractError(
            f"Qlib version {actual_qlib_version} does not match the trained runtime "
            f"{expected_qlib_version}"
        )


def score_decision_dates(
    *,
    export_root: Path | str,
    dataset_name: str,
    export_generation_id: str,
    run_generation_id: str,
    artifact_generation_id: str,
    decision_dates: Iterable[str],
    provider_uri: str | None = None,
    expected_qlib_version: str | None = None,
) -> InferenceResult:
    """Return one `{instrument, score}` frame per requested decision date.

    The caller owns everything about *which* dates matter; this function owns
    loading the governed model and refusing anything that does not line up.
    """
    root = Path(export_root)
    snapshot = root / f"dataset={dataset_name}" / f"generation={export_generation_id}"
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.is_file():
        raise ContractError(f"unpublished or incomplete Qlib export: {snapshot}")
    export_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ModelContractLoader.validate("qlib_dataset_export", export_manifest)
    if export_manifest["generation_id"] != export_generation_id:
        raise ContractError("Qlib export path generation does not match its manifest")
    if provider_uri is not None:
        import hashlib

        if export_manifest.get("provider_uri_sha256") != hashlib.sha256(
            provider_uri.encode("utf-8")
        ).hexdigest():
            raise ContractError("Qlib export was not produced for this provider URI")
    for entry in export_manifest["files"]:
        path = snapshot / entry["path"]
        if (
            not path.is_file()
            or file_sha256_bytes(path.read_bytes()) != entry["checksum_sha256"]
            or path.stat().st_size != entry["byte_size"]
        ):
            raise ContractError(f"tampered Qlib export file: {entry['path']}")

    mapping_path = snapshot / "feature_mapping.json"
    feature_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if "label" not in feature_mapping:
        raise ContractError("Qlib export feature mapping carries no label column")
    features = [name for name in feature_mapping if name != "label"]
    if not features:
        raise ContractError("Qlib export feature mapping carries no features")

    artifact_store = ArtifactStore(root.parent)
    artifact_manifest, artifact_bytes = artifact_store.read(
        run_generation_id, artifact_generation_id
    )
    runtime_path = artifact_manifest["runtime_import_path"]
    is_qlib_linear = runtime_path == "qlib.contrib.model.linear.LinearModel"
    is_qlib_lightgbm = runtime_path == "lightgbm.sklearn.LGBMRegressor"
    if not (is_qlib_linear or is_qlib_lightgbm):
        raise ContractError(
            "inference only loads reviewed qlib_linear or qlib_lightgbm runtimes, got "
            f"{runtime_path}"
        )
    # Qlib is an optional extra: the refusal paths above must stay testable in an
    # environment without it, so the import happens only once a caller has asked
    # for a real inference run.
    try:
        import qlib
        from qlib.data.dataset import DatasetH
        from qlib.data.dataset.handler import DataHandlerLP
        from qlib.data.dataset.loader import QlibDataLoader
    except ImportError as exc:
        raise ContractError(
            "Qlib is required to score decision dates; install the qlib extra"
        ) from exc
    assert_runtime_version(qlib.__version__, expected_qlib_version)
    if is_qlib_lightgbm:
        try:
            import lightgbm
        except ImportError as exc:
            raise ContractError("LightGBM is required to score this artifact; install the gbdt extra") from exc
        if artifact_manifest["runtime_name"] != f"qlib_lightgbm:{lightgbm.__version__}":
            raise ContractError("LightGBM runtime version does not match the trained artifact")

    calendar = [line for line in (snapshot / "calendars" / "day.txt").read_text(
        encoding="utf-8"
    ).splitlines() if line]
    if not calendar:
        raise ContractError("Qlib export calendar is empty")
    wanted = sorted(set(decision_dates))
    missing = [day for day in wanted if day not in set(calendar)]
    if missing:
        raise ContractError(
            f"decision dates outside the exported calendar: {missing}; "
            f"calendar spans {calendar[0]}..{calendar[-1]}"
        )
    if not wanted:
        raise ContractError("inference requires at least one decision date")

    instruments = sorted(
        line.split("\t", 1)[0]
        for line in (snapshot / "instruments" / "all.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    )
    if not instruments:
        raise ContractError("Qlib export instruments are empty")

    qlib.init(
        provider_uri=str(snapshot),
        region="cn",
        expression_cache=None,
        dataset_cache=None,
        kernels=1,
        logging_level="WARNING",
    )
    loader = QlibDataLoader(
        config={
            "feature": [f"${feature_mapping[name]}" for name in features],
            "label": [f"${feature_mapping['label']}"],
        },
        freq="day",
    )
    handler = DataHandlerLP(
        instruments=instruments,
        start_time=calendar[0],
        end_time=calendar[-1],
        data_loader=loader,
        infer_processors=[],
        learn_processors=[],
        process_type="append",
    )
    dataset = DatasetH(
        handler, segments={"infer": (wanted[0], wanted[-1])}
    )

    model = _load_model(artifact_bytes, artifact_manifest)
    if is_qlib_linear:
        predictions = model.predict(dataset, segment="infer")
    else:
        feature_frame = dataset.prepare(
            "infer", col_set="feature", data_key=DataHandlerLP.DK_I
        )
        values = feature_frame.to_numpy(dtype="float64", na_value=np.nan)
        scorable = np.isfinite(values).all(axis=1)
        scores = np.full(len(feature_frame), np.nan, dtype="float64")
        if scorable.any():
            scores[scorable] = model.predict(feature_frame.loc[scorable])
        predictions = pd.Series(scores, index=feature_frame.index)
    if predictions.empty:
        raise ContractError("inference produced no scores")

    frame = predictions.rename("score").reset_index()
    # Qlib returns a (datetime, instrument) MultiIndex with lowercased ids.
    frame["instrument"] = frame["instrument"].astype(str).str.upper()
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
    # Qlib enumerates instruments by the span recorded in `instruments/all.txt`,
    # so a name that has no row on a given session (a suspension, or a hole in
    # the canonical panel) still comes back with an all-NaN feature vector and
    # therefore a NaN score. Those rows do not exist in the training frame either,
    # so scoring them would be inventing coverage. Drop them and report the count
    # rather than silently publishing a partial set.
    unscorable = int(frame["score"].isna().sum())
    frame = frame[frame["score"].notna()]
    if frame.empty:
        raise ContractError("inference produced no finite scores")
    if frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("inference produced duplicate (instrument, datetime) keys")

    scored: dict[str, pd.DataFrame] = {}
    for decision_date in wanted:
        day = frame[frame["datetime"] == decision_date]
        if day.empty:
            raise ContractError(
                f"inference produced no scores for decision date {decision_date}"
            )
        scores = day[["instrument", "score"]].reset_index(drop=True)
        scores["score"] = scores["score"].astype("float64")
        scored[decision_date] = scores.sort_values(
            "instrument", kind="mergesort"
        ).reset_index(drop=True)
    return InferenceResult(scores=scored, unscorable_rows=unscorable)


def _load_model(artifact_bytes: bytes, manifest: Mapping[str, Any]) -> Any:
    """Load the artifact only after its manifest identity has been verified."""
    if manifest.get("serialization_profile_id") != "joblib-v1":
        raise ContractError(
            f"unsupported model serialization profile: {manifest.get('serialization_profile_id')}"
        )
    import io

    try:
        model = joblib.load(io.BytesIO(artifact_bytes))
    except Exception as exc:  # noqa: BLE001 - any unpickle failure is a refusal
        raise ContractError(f"model artifact could not be loaded: {exc}") from exc
    if not hasattr(model, "predict"):
        raise ContractError("loaded model artifact has no predict method")
    return model
