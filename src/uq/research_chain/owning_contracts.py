from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.artifacts import QualityReportStore
from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.manifest import validate_canonical_v2_manifest
from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    model_manifest_identities,
    resolve_subject_quality_decision,
    sha256_json,
)
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..factors.raw_price import logical_fingerprint
from ..models.labels import label_row_checksum


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


class BacktestConfigStore:
    """Publish and read immutable governed backtest-config manifests.

    The backtest stage only ever read this family, so a v5 execute had no way to
    produce the ``backtest_config_binding`` its request declares -- the family had
    no write side at all. Publishing follows the two-step identity the other
    owning stores use (``TargetWeightStore.publish``): derive a provisional
    generation without the review checksum, bind the reviewed quality decision to
    it, then re-derive the final identity *with* the checksum participating, so the
    review attests the exact content that gets published.
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

        provisional, _ = model_manifest_identities(
            {**payload, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="backtest_config",
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
            schema_name="backtest_config",
        )
        payload["generation_id"] = final
        payload["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("backtest_config", payload)

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
                ModelContractLoader.validate("backtest_config", manifest)
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
