from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Sequence, Mapping

import pandas as pd

from ..contracts.artifacts import QualityReportStore
from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.gate_contracts import sha256_json, validate_contract
from ..contracts.manifest import validate_canonical_v2_manifest
from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    model_manifest_identities,
    resolve_subject_quality_decision,
    reviewed_allowed_statuses,
    sha256_json,
)
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..factors.raw_price import logical_fingerprint
from ..models.labels import label_row_checksum
from ..models.sample_construction import serialize_sample_frame


class FeatureSchemaStore:
    """Publish and read durable feature-schema contracts.

    The dataset stage reads the input/output schemas a preprocessing manifest points
    at, but nothing wrote them: a v5 execute could not resolve
    ``input_feature_schema_generation_id`` because the family had no write side.
    A feature schema carries no review, so its identity is its own -- the builder
    derives it and this only persists it immutably.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        return _read_manifest(self.root / "feature_schemas", generation_id, "feature_schema")

    def read_schema(self, generation_id: str) -> dict[str, Any]:
        return self.read_manifest(generation_id)

    def publish(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Publish one feature-schema manifest and return it with its identity.

        ``FeatureSchemaBuilder.build`` derives ``generation_id`` and hands the
        schema to its consumer with that id already in place, so the caller cannot
        strip it. The identity is re-derived here and the inbound value is only
        checked to agree: a mismatch is a caller bug, not something to overwrite.
        """
        payload = {key: value for key, value in manifest.items()
                   if key not in {"generation_id", "manifest_digest_sha256"}}
        # model_manifest_identities needs the identity keys present (it strips them
        # itself), so it is called with placeholders, exactly as the other families do.
        payload["generation_id"] = "0" * 64
        payload["manifest_digest_sha256"] = "0" * 64
        generation, digest = model_manifest_identities(payload, schema_name="feature_schema")
        inbound = manifest.get("generation_id")
        if inbound not in (None, "0" * 64) and inbound != generation:
            raise ContractError(
                "feature schema generation_id does not match the derived identity; "
                "the builder must derive it, not the caller"
            )
        payload["generation_id"] = generation
        payload["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("feature_schema", payload)

        directory = self.root / "feature_schemas" / f"generation={generation}"
        if directory.exists():
            raise ContractError(f"immutable feature schema already published: {directory}")
        staging = self.root / "feature_schemas" / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            (staging / "manifest.json").write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            staging.replace(directory)
        except Exception:
            for path in staging.glob("*"):
                path.unlink()
            staging.rmdir()
            raise
        return payload


class AdjustedPriceDatasetStore:
    """Read an immutable canonical-v2 adjusted-price manifest by generation."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        manifest, _ = self._read_verified_directory(generation_id)
        return manifest

    def read_frame(self, generation_id: str) -> pd.DataFrame:
        manifest, directory = self._read_verified_directory(generation_id)
        return pd.read_parquet(directory / "data.parquet")

    def data_path(self, generation_id: str) -> Path:
        _, directory = self._read_verified_directory(generation_id)
        return directory / "data.parquet"

    def list_partitions(self) -> list[dict[str, Any]]:
        """Every published adjusted-v2 partition as an ordered set triple.

        ``AdjustedPriceDatasetStore`` has always been single-partition, because
        the per-session invariants that make the line meaningful (`adj_factor`
        positive, restatement equals the vendor value, jump agreement with the
        prior partition) are all *between* adjacent partitions -- cr.md 8.63 keeps
        that publish granularity.  This method adds nothing but enumeration, so a
        `backtest_config.v2` `price_source_binding` can name the whole window
        without either merging partitions into a second representation of the
        same bytes or loosening `canonical_v2`'s `partition_date: date`.

        The triples are the same `{session_or_decision_date, generation_id,
        data_checksum_sha256}` shape `set_digest_sha256` and
        `verify_partition_set_binding` consume for factor partitions and paper
        bindings, so the digest convention is shared rather than re-invented.
        """
        base = self.root / "canonical" / "bars_adjusted" / "adjusted-v2"
        if not base.is_dir():
            return []
        triples: list[dict[str, Any]] = []
        for child in sorted(base.iterdir()):
            if not child.is_dir() or not child.name.startswith("date="):
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            triples.append({
                "session_or_decision_date": child.name.split("=", 1)[1],
                "generation_id": manifest.get("generation_id"),
                "data_checksum_sha256": manifest.get("data_checksum_sha256"),
            })
        return sorted(triples, key=lambda item: str(item["session_or_decision_date"]))

    def list_partitions_in_window(
        self, window_start: date | str, window_end: date | str
    ) -> list[dict[str, Any]]:
        """The published partitions whose session falls inside the bound window.

        A store is expected to retain history: a backtest config names the
        sessions it will trade, not every session that happens to be published
        under the same root.  Filtering here (rather than at each caller) keeps
        the publisher's set digest and the backtest's readback on the same
        definition -- otherwise a config built for ``[start, end]`` would be
        rejected as soon as the store also held an earlier baseline partition.
        """
        start = window_start if isinstance(window_start, date) else date.fromisoformat(str(window_start))
        end = window_end if isinstance(window_end, date) else date.fromisoformat(str(window_end))
        if end < start:
            raise ContractError("adjusted price window end precedes start")
        return [
            triple for triple in self.list_partitions()
            if start <= date.fromisoformat(str(triple["session_or_decision_date"])) <= end
        ]

    def read_partition_frames(
        self, triples: Sequence[Mapping[str, Any]]
    ) -> pd.DataFrame:
        """Read a partition set into one frame, verifying each member's bytes.

        The join is the only place a window frame exists, and it is derived at read
        time from the very partitions the binding names -- so there is no second
        stored representation to drift.  Three invariants are enforced, because a
        backtest derives its calendar from this frame:

        * every triple must resolve through `read_frame`, which re-verifies the
          bytes against the partition's manifest, so a tampered member is refused
          rather than concatenated;
        * the row count must equal the sum of the members' -- a duplicate or lost
          member cannot pass silently;
        * `(instrument, session_date)` must be unique -- a repeated key would
          double-count an instrument's exposure in the mark-to-market pass.
        """
        if not triples:
            raise ContractError("adjusted price partition set is empty")
        frames: list[pd.DataFrame] = []
        expected_rows = 0
        for triple in triples:
            frame = self.read_frame(str(triple["generation_id"]))
            expected_rows += len(frame)
            frames.append(frame)
        joined = pd.concat(frames, ignore_index=True)
        if len(joined) != expected_rows:
            raise ContractError(
                f"adjusted price row count drifted across partitions: {len(joined)} != {expected_rows}"
            )
        session_column = "session_date" if "session_date" in joined.columns else "datetime"
        if joined.duplicated(subset=["instrument", session_column]).any():
            duplicates = joined.loc[
                joined.duplicated(subset=["instrument", session_column]),
                ["instrument", session_column],
            ].head(5)
            raise ContractError(
                "duplicate (instrument, session) keys across adjusted price partitions: "
                + duplicates.to_json(orient="records")
            )
        return joined

    def _read_verified_directory(self, generation_id: str) -> tuple[dict[str, Any], Path]:
        _validate_generation_id(generation_id, "adjusted price")
        candidates = _manifests_under(self.root / "canonical")
        matches: list[Path] = []
        for path in candidates:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # A document that is not an object cannot name a generation, so it is
            # not a candidate; it stays in the "unpublished" branch rather than
            # escaping as an AttributeError from a non-dict .get().
            if not isinstance(manifest, dict) or manifest.get("generation_id") != generation_id:
                continue
            try:
                validate_canonical_v2_manifest(manifest)
            except ContractError as exc:
                # cr.md contracts MINOR-2: a manifest that names the queried
                # generation but fails validation is tampering, not "unpublished"
                # -- the diagnostics must say so loudly.
                raise ContractError(
                    f"invalid manifest for published adjusted price generation: {generation_id}"
                ) from exc
            matches.append(path.parent)
        if not matches:
            raise ContractError(f"unpublished adjusted price dataset: {generation_id}")
        if len(matches) > 1:
            raise ContractError(f"ambiguous adjusted price dataset generation: {generation_id}")
        return self._read_verified_manifest(matches[0]), matches[0]

    def _read_verified_manifest(self, directory: Path) -> dict[str, Any]:
        # Schema plus all three identities come from the one contract-layer rule,
        # so this boundary cannot drift from the publisher or the governed reader.
        manifest = validate_canonical_v2_manifest(_read_json(directory / "manifest.json"))
        data_path = directory / "data.parquet"
        if not data_path.is_file():
            raise ContractError("incomplete adjusted price dataset")
        if manifest["data_checksum_sha256"] != file_sha256_bytes(data_path.read_bytes()):
            raise ContractError("tampered adjusted price data checksum")
        if not manifest.get("quality_report_checksum"):
            raise ContractError("adjusted price dataset is missing quality report binding")
        try:
            canonical_root = next(parent for parent in directory.parents if parent.name == "canonical")
        except StopIteration as exc:
            raise ContractError("invalid adjusted price dataset store layout") from exc
        # ``directory`` was enumerated from ``self.root / "canonical"``, so it is
        # spelled relative to the store root -- keep it that way.  Resolving here
        # would collapse an in-store link before ``reject_symlinks_within`` can
        # see it, and (when the root is spelled through macOS's ``/tmp`` shim)
        # hand the guard a resolved path beside an unresolved root.  Containment
        # is compared on resolved forms; the link audit walks the spelled path.
        if not canonical_root.resolve().is_relative_to(self.root.resolve()):
            raise ContractError("unsafe adjusted price dataset store layout")
        reject_symlinks_within(self.root, canonical_root, label="adjusted price dataset root")
        report_root = canonical_root.parent
        report_path = report_root / "reports" / "canonical_v2" / manifest["generation_id"] / "report.json"
        if not report_path.is_file():
            raise ContractError("adjusted price quality report is missing")
        QualityReportStore().read(
            report_root,
            manifest["generation_id"],
            binding_type="canonical_v2",
        )
        if file_sha256_bytes(report_path.read_bytes()) != manifest["quality_report_checksum"]:
            raise ContractError("adjusted price quality report checksum mismatch")
        return manifest


class BacktestMarketDatasetStore:
    """Read immutable market-data artifacts referenced by backtest config bindings."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_frame(
        self,
        generation_id: str,
        expected_checksum_sha256: str,
        *,
        dataset_type: str,
        required_columns: set[str] | None = None,
    ) -> pd.DataFrame:
        _validate_generation_id(generation_id, f"{dataset_type} dataset")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_checksum_sha256):
            raise ContractError(f"invalid {dataset_type} dataset checksum")
        directory = self.root / "backtest_market_data" / f"dataset={generation_id}"
        if not directory.is_dir():
            raise ContractError(f"unpublished {dataset_type} dataset: {generation_id}")
        manifest = _read_json(directory / "manifest.json")
        if manifest.get("generation_id") != generation_id:
            raise ContractError(f"{dataset_type} dataset manifest identity mismatch")
        if manifest.get("dataset_type") != dataset_type:
            raise ContractError(f"{dataset_type} dataset manifest type mismatch")
        data_path = directory / "data.parquet"
        if not data_path.is_file():
            raise ContractError(f"incomplete {dataset_type} dataset: {generation_id}")
        actual_checksum = file_sha256_bytes(data_path.read_bytes())
        if actual_checksum != expected_checksum_sha256:
            raise ContractError(f"tampered {dataset_type} dataset checksum")
        if manifest.get("data_checksum_sha256") != expected_checksum_sha256:
            raise ContractError(f"{dataset_type} dataset manifest checksum mismatch")
        frame = pd.read_parquet(data_path)
        if list(frame.columns) != manifest.get("columns"):
            raise ContractError(f"{dataset_type} dataset column mismatch")
        if len(frame) != manifest.get("row_count"):
            raise ContractError(f"{dataset_type} dataset row count mismatch")
        if required_columns is not None and not required_columns.issubset(frame.columns):
            raise ContractError(f"{dataset_type} dataset is missing required columns")
        return frame

    def publish(
        self,
        frame: pd.DataFrame,
        *,
        dataset_type: str,
        window_start: date,
        window_end: date,
        sources: Mapping[str, str],
        producer: str,
    ) -> dict[str, Any]:
        """Publish one whole-window market dataset for a backtest config binding.

        `read_frame` has always had a writer-less counterpart: this store's layout was
        written only by test helpers, so no real run could supply the calendar,
        suspension or corporate-action input that `backtest_config.v1` binds
        (a single `generation_id` + `checksum_sha256`, never a partition set).
        This is that publisher, and it keeps the same contract `read_frame`
        verifies: one immutable `backtest_market_data/dataset=<generation>`
        directory whose generation id is a content hash of the frame's own bytes.

        `sources` records provenance rather than being consumed, so the split the
        paper plan mandates stays visible on the artifact: for suspensions the bars
        `status` column is the primary source and `suspend_d` only reconciles, never
        becoming a second truth.

        A window already published byte-identically is reported as skipped rather
        than refused, so an interrupted run resumes (every writer here is
        immutable, which is the tamper guard, not a bug).
        """
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ContractError(f"{dataset_type} dataset requires a non-empty frame")
        for column in ("date", "instrument"):
            if column not in frame.columns:
                raise ContractError(f"{dataset_type} dataset requires a {column} column")
        ordered = frame.sort_values(["date", "instrument"], kind="mergesort").reset_index(drop=True)
        generation = sha256_json({
            "dataset_type": dataset_type,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "columns": list(ordered.columns),
            "rows": ordered.to_dict(orient="records"),
        })
        directory = self.root / "backtest_market_data" / f"dataset={generation}"
        if directory.is_dir():
            # cr.md 8.62: the same window re-run is the resume path, so an identical
            # re-publish is a skip, not a failure. A *different* frame that hashes to
            # the same generation is impossible by construction of the content hash.
            return self.read_manifest_by(generation, dataset_type=dataset_type)
        staging = self.root / "backtest_market_data" / f".staging_{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True)
            data_path = staging / "data.parquet"
            ordered.to_parquet(data_path, index=False)
            data_checksum = file_sha256_bytes(data_path.read_bytes())
            manifest = {
                "dataset_version": "1.0.0",
                "dataset_type": dataset_type,
                "generation_id": generation,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "columns": list(ordered.columns),
                "row_count": len(ordered),
                "data_checksum_sha256": data_checksum,
                "sources": dict(sources),
                "producer": producer,
                "serialization_profile": "parquet-v1",
            }
            manifest["manifest_digest_sha256"] = sha256_json(manifest)
            validate_contract("backtest_market_dataset.v1.json", manifest)
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            reject_symlinks_within(self.root, directory.parent, label="backtest market data path")
            fsync_tree(staging)
            os.replace(staging, directory)
            fsync_dir(directory.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return manifest

    def read_manifest_by(self, generation_id: str, *, dataset_type: str) -> dict[str, Any]:
        """Read a published backtest market dataset's manifest without a checksum."""
        _validate_generation_id(generation_id, f"{dataset_type} dataset")
        path = self.root / "backtest_market_data" / f"dataset={generation_id}" / "manifest.json"
        if not path.is_file():
            raise ContractError(f"unpublished {dataset_type} dataset: {generation_id}")
        manifest = _read_json(path)
        if manifest.get("generation_id") != generation_id or manifest.get("dataset_type") != dataset_type:
            raise ContractError(f"{dataset_type} dataset manifest identity mismatch")
        return manifest


class LabelStore:
    """Manifest-first read boundary for durable label-set artifacts."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        return _read_manifest(self.root / "label_sets", generation_id, "label_set")

    def read_frame(self, generation_id: str) -> tuple[dict[str, Any], pd.DataFrame]:
        manifest = self.read_manifest(generation_id)
        directory = _match_directory(self.root / "label_sets", generation_id)
        data_path = directory / "data.parquet"
        if not data_path.is_file():
            raise ContractError(f"unpublished label data: {generation_id}")
        # cr.md: the manifest declares data_checksum_sha256 over the label rows, so
        # a frame whose values were edited after publication must be refused. The
        # shape checks below only cover columns, row count and dtypes, none of which
        # moves when a label value is rewritten.
        frame = pd.read_parquet(data_path)
        if frame.columns.tolist() != manifest["columns"]:
            raise ContractError("label artifact column mismatch")
        if len(frame) != manifest["row_count"]:
            raise ContractError("label artifact row count mismatch")
        actual_dtypes = {key: str(value) for key, value in frame.dtypes.items()}
        expected_dtypes = {key: "datetime64[ns]" if value.startswith("datetime") else value for key, value in manifest["dtypes"].items()}
        if actual_dtypes != expected_dtypes:
            raise ContractError("label artifact dtype mismatch")
        # The label manifest declares data_checksum_sha256 exactly as
        # `models/labels.py` computes it, so call that single definition rather
        # than reproducing the expression here: the two copies used to drift
        # independently (cr.md OBS-F). It is deliberately not
        # logical_fingerprint -- the two fields are different functions and only
        # this one is the row checksum the reader verifies.
        actual_checksum = label_row_checksum(frame)
        if actual_checksum != manifest["data_checksum_sha256"]:
            raise ContractError(
                "label artifact checksum mismatch; the published rows were modified after publication"
            )
        return manifest, frame


class FeaturePreprocessingStore:
    """Publish and read durable feature-preprocessing contracts.

    ``FeaturePreprocessorBuilder.build`` produces the manifest but nothing wrote it,
    so a v5 execute could not produce the ``feature_preprocessing_binding`` its
    request declares. The builder already derives the identity with
    ``input_feature_schema_manifest_digest_sha256`` and
    ``quality_report_checksum_sha256`` excluded -- the digest of a schema that is
    itself published under a review cannot be known before the review exists --
    so publishing only has to bind the review and re-derive with it participating.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        manifest = _read_manifest(self.root / "feature_preprocessing", generation_id, "feature_preprocessing")
        if manifest.get("quality_report_checksum_sha256") in (None, "0" * 64):
            raise ContractError("feature preprocessing is missing a quality report binding")
        return manifest

    def publish(
        self,
        manifest: dict[str, Any],
        *,
        quality_decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish one preprocessing manifest and return it with its identity.

        ``manifest`` comes from ``FeaturePreprocessorBuilder.build``, which already
        carries a derived ``generation_id`` for the review to attest. The review is
        bound to that generation and the manifest is re-validated afterwards: unlike
        the backtest-config family, the identity here *excludes* the two fields that
        cannot exist before publishing, so the generation the reviewer signs is the
        final one and must not change.
        """
        generation = manifest.get("generation_id")
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
            raise ContractError(
                "feature preprocessing manifest must carry the generation derived by the builder"
            )
        if manifest.get("generation_id") == "0" * 64:
            raise ContractError("feature preprocessing manifest has an unresolved generation_id")

        # The one-argument provider form (the shape this repo already uses) commits
        # the content digest to the generation id, so pass the generation id as the
        # content digest rather than the frame hash -- otherwise the signature can
        # never match. The frame hash still participates in the generation itself.
        quality_decision = resolve_subject_quality_decision(
            quality_decision,
            subject_generation_id=generation,
            subject_content_sha256=generation,
        )
        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="feature_preprocessing_v1",
            subject_generation_id=generation,
            subject_content_sha256=generation,
        )
        published = dict(manifest)
        published["quality_report_checksum_sha256"] = report_checksum
        # The generation excludes the review checksum but the *manifest digest*
        # includes it, so the digest the builder derived (before the review existed)
        # is stale once we bind one. Re-derive: generation must not move, digest must.
        exclude = {"input_feature_schema_manifest_digest_sha256",
                   "quality_report_checksum_sha256"}
        regenerated, digest = model_manifest_identities(
            published, schema_name="feature_preprocessing", exclude_fields=exclude
        )
        if regenerated != generation:
            raise ContractError(
                "feature preprocessing generation moved when the review was bound"
            )
        published["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("feature_preprocessing", published)

        directory = self.root / "feature_preprocessing" / f"generation={generation}"
        if directory.exists():
            raise ContractError(f"immutable feature preprocessing already published: {directory}")
        staging = self.root / "feature_preprocessing" / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            (staging / "manifest.json").write_text(
                json.dumps(published, sort_keys=True) + "\n", encoding="utf-8"
            )
            (self.root / "external_quality_reviews").mkdir(parents=True, exist_ok=True)
            (self.root / "external_quality_reviews" / f"{report_checksum}.json").write_text(
                json.dumps(bound_report, sort_keys=True) + "\n", encoding="utf-8"
            )
            staging.replace(directory)
        except Exception:
            for path in staging.glob("*"):
                path.unlink()
            staging.rmdir()
            raise
        return published


class SampleConstructionStore:
    """Publish and read the immutable training-sample selection.

    This family exists so label-aware row selection cannot leak into
    `feature_preprocessing.v1`. It binds the factor/label/universe inputs, the
    selected frame and the selection audit; preprocessing then receives the
    selected feature frame and remains a stateless transform.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        manifest = _read_manifest(
            self.root / "sample_construction", generation_id, "sample_construction"
        )
        if manifest.get("quality_report_checksum_sha256") in (None, "0" * 64):
            raise ContractError("sample construction is missing a quality report binding")
        checksum = manifest["quality_report_checksum_sha256"]
        report_path = self.root / "external_quality_reviews" / f"{checksum}.json"
        if not report_path.is_file():
            raise ContractError("sample construction quality report is unavailable")
        report = _read_json(report_path)
        ModelContractLoader.validate("model_quality_report", report)
        if (
            sha256_json({
                key: value for key, value in report.items()
                if key != "report_checksum_sha256"
            }) != checksum
            or report.get("binding_type") != "sample_construction_v1"
            or report.get("bound_generation_id") != generation_id
            or report.get("subject_content_sha256") != generation_id
        ):
            raise ContractError("sample construction quality report rejects read")
        if report.get("status") not in reviewed_allowed_statuses("sample_construction_v1"):
            raise ContractError("sample construction quality report status rejects read")
        return manifest

    def read_frame(self, generation_id: str) -> tuple[dict[str, Any], pd.DataFrame]:
        manifest = self.read_manifest(generation_id)
        directory = _match_directory(
            self.root / "sample_construction", generation_id
        )
        data_path = directory / "data.parquet"
        if not data_path.is_file():
            raise ContractError(f"unpublished sample construction data: {generation_id}")
        frame = pd.read_parquet(data_path)
        if list(frame.columns) != manifest["columns"]:
            raise ContractError("sample construction column mismatch")
        if len(frame) != manifest["row_count"]:
            raise ContractError("sample construction row count mismatch")
        if file_sha256_bytes(data_path.read_bytes()) != manifest["data_checksum_sha256"]:
            raise ContractError("tampered sample construction data checksum")
        if logical_fingerprint(frame) != manifest["logical_fingerprint"]:
            raise ContractError("sample construction logical fingerprint mismatch")
        return manifest, frame

    def publish(
        self,
        manifest: dict[str, Any],
        frame: pd.DataFrame,
        *,
        quality_decision: dict[str, Any],
    ) -> dict[str, Any]:
        generation = manifest.get("generation_id")
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
            raise ContractError("sample construction manifest lacks a derived generation")
        if manifest.get("generation_id") == "0" * 64:
            raise ContractError("sample construction generation is unresolved")
        if list(frame.columns) != manifest["columns"] or len(frame) != manifest["row_count"]:
            raise ContractError("sample construction frame does not match its manifest")
        artifact, data_checksum = serialize_sample_frame(frame)
        if data_checksum != manifest["data_checksum_sha256"]:
            raise ContractError("sample construction frame checksum mismatch")
        quality_decision = resolve_subject_quality_decision(
            quality_decision,
            subject_generation_id=generation,
            subject_content_sha256=generation,
        )
        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="sample_construction_v1",
            subject_generation_id=generation,
            subject_content_sha256=generation,
        )
        published = dict(manifest)
        published["quality_report_checksum_sha256"] = report_checksum
        regenerated, digest = model_manifest_identities(
            published,
            schema_name="sample_construction",
            exclude_fields={"quality_report_checksum_sha256"},
        )
        if regenerated != generation:
            raise ContractError("sample construction generation moved when review was bound")
        published["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("sample_construction", published)
        directory = self.root / "sample_construction" / f"generation={generation}"
        if directory.exists():
            raise ContractError(f"immutable sample construction already published: {directory}")
        staging = self.root / "sample_construction" / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            data_path = staging / "data.parquet"
            data_path.write_bytes(artifact)
            (staging / "manifest.json").write_text(
                json.dumps(published, sort_keys=True) + "\n", encoding="utf-8"
            )
            (self.root / "external_quality_reviews").mkdir(parents=True, exist_ok=True)
            (self.root / "external_quality_reviews" / f"{report_checksum}.json").write_text(
                json.dumps(bound_report, sort_keys=True) + "\n", encoding="utf-8"
            )
            fsync_tree(staging)
            staging.replace(directory)
        except Exception:
            for path in staging.glob("*"):
                path.unlink()
            staging.rmdir()
            raise
        return published


class BacktestConfigStore:
    """Publish and read immutable governed backtest-config manifests.

    The backtest stage only ever read this family, so a v5 execute had no way to
    produce the ``backtest_config_binding`` its request declares -- the family had
    no write side at all. Publishing follows the two-step identity the other
    owning stores use (``TargetWeightStore.publish``): derive a provisional
    generation without the review checksum, bind the reviewed quality decision to
    it, then re-derive the final identity *with* the checksum participating, so the
    review attests the exact content that gets published.

    ``contract_version`` selects the contract variant: 1 (default) keeps the
    original single-generation ``price_source_binding``; 2 uses the partition-set
    form (cr.md 8.63).  The *review binding type* stays ``backtest_config_v1`` for
    both, because it names the reviewer's registered policy for this family rather
    than the manifest's shape -- the operator's quality-decision document is
    therefore the same for either version.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def publish(
        self,
        manifest: dict[str, Any],
        *,
        quality_decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish one backtest-config manifest and return it with its identity.

        ``manifest`` supplies every field except ``generation_id`` /
        ``manifest_digest_sha256`` / ``quality_report_checksum_sha256``, which are
        derived here. A manifest that already carries a non-placeholder
        ``generation_id`` is refused: the identity is a function of the content,
        so an inbound value would silently select a generation the caller chose.
        """
        for field in ("generation_id", "manifest_digest_sha256"):
            value = manifest.get(field)
            if value not in (None, "0" * 64):
                raise ContractError(
                    f"backtest config manifest must not carry a {field}; it is derived on publish"
                )
        payload = {key: value for key, value in manifest.items()
                   if key not in {"generation_id", "manifest_digest_sha256"}}

        # cr.md 8.63: v2 changes `price_source_binding` from a single generation to
        # a partition set.  The version comes from the payload, so a caller
        # publishing the old shape keeps the old contract verbatim -- v1 is never
        # edited in place, which is the repo's semantic/unit rule.
        contract_version = payload.get("contract_version")
        if contract_version in (None, 1):
            schema_name = "backtest_config"
        elif contract_version == 2:
            schema_name = "backtest_config_v2"
        else:
            raise ContractError(
                f"unsupported backtest_config contract_version: {contract_version}"
            )

        provisional, _ = model_manifest_identities(
            {**payload, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name=schema_name,
        )
        # The subject content digest is the provisional generation id itself: this
        # family has no data partition, so the manifest *is* the artifact, and the
        # one-argument provider form (which this repo already uses for
        # backtest_config) commits to exactly that default.
        quality_decision = resolve_subject_quality_decision(
            quality_decision,
            subject_generation_id=provisional,
            subject_content_sha256=provisional,
        )
        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="backtest_config_v1",
            subject_generation_id=provisional,
            subject_content_sha256=provisional,
        )
        payload["quality_report_checksum_sha256"] = report_checksum
        final, digest = model_manifest_identities(
            {**payload, "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name=schema_name,
        )
        payload["generation_id"] = final
        payload["manifest_digest_sha256"] = digest
        ModelContractLoader.validate(schema_name, payload)

        directory = self.root / "backtest_configs" / f"generation={final}"
        if directory.exists():
            raise ContractError(f"immutable backtest config already published: {directory}")
        staging = self.root / "backtest_configs" / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            (staging / "manifest.json").write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            (self.root / "external_quality_reviews").mkdir(parents=True, exist_ok=True)
            (self.root / "external_quality_reviews" / f"{report_checksum}.json").write_text(
                json.dumps(bound_report, sort_keys=True) + "\n", encoding="utf-8"
            )
            staging.replace(directory)
        except Exception:
            for path in staging.glob("*"):
                path.unlink()
            staging.rmdir()
            raise
        return payload

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        _validate_generation_id(generation_id, "backtest config")
        candidates = _manifests_under(self.root / "backtest_configs")
        matches: list[Path] = []
        for path in candidates:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                # cr.md 8.63: the family now has two manifest shapes -- v1's single
                # `price_source_binding` generation and v2's partition set.  The
                # manifest names its own `contract_version`, so validation follows
                # it; hardcoding one schema made a published v2 config unreadable.
                ModelContractLoader.validate(
                    "backtest_config_v2" if manifest.get("contract_version") == 2
                    else "backtest_config",
                    manifest,
                )
            except (OSError, json.JSONDecodeError, ContractError):
                continue
            if manifest.get("generation_id") == generation_id:
                matches.append(path.parent)
        if not matches:
            raise ContractError(f"unpublished backtest config: {generation_id}")
        if len(matches) > 1:
            raise ContractError(f"ambiguous backtest config generation: {generation_id}")
        return json.loads((matches[0] / "manifest.json").read_text(encoding="utf-8"))


def _validate_generation_id(generation_id: str, family: str) -> None:
    if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{64}", generation_id):
        raise ContractError(f"invalid {family} generation id")


def _manifests_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [
        path for path in root.rglob("manifest.json")
        if not any(part.startswith(".") for part in path.parent.relative_to(root).parts)
    ]


def _match_directory(root: Path, generation_id: str) -> Path:
    if len(generation_id) != 64:
        raise ContractError("invalid owning artifact generation id")
    if not root.exists():
        raise ContractError(f"unpublished owning artifact root: {root}")
    candidates = [
        path.parent
        for path in root.rglob("manifest.json")
        if not any(part.startswith(".") for part in path.parent.relative_to(root).parts)
    ]
    matches = []
    for candidate in candidates:
        try:
            document = json.loads((candidate / "manifest.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if document.get("generation_id") == generation_id:
            matches.append(candidate)
    if not matches:
        raise ContractError(f"unpublished owning artifact: {generation_id}")
    if len(matches) > 1:
        raise ContractError(f"ambiguous owning artifact generation: {generation_id}")
    return matches[0]


def _read_manifest(root: Path, generation_id: str, schema_name: str) -> dict[str, Any]:
    manifest = _read_json(_match_directory(root, generation_id) / "manifest.json")
    ModelContractLoader.validate(schema_name, manifest)
    return manifest


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("malformed owning artifact manifest") from exc
    return payload
