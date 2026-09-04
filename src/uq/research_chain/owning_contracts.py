from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.artifacts import QualityReportStore
from ..contracts.canonical_v2 import canonical_v2_identities, file_sha256_bytes
from ..contracts.gate_contracts import validate_contract
from ..contracts.model_layer import ModelContractLoader
from ..errors import ContractError
from ..factors.raw_price import logical_fingerprint


class FeatureSchemaStore:
    """Manifest-first read boundary for durable feature-schema contracts."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        return _read_manifest(self.root / "feature_schemas", generation_id, "feature_schema")

    def read_schema(self, generation_id: str) -> dict[str, Any]:
        return self.read_manifest(generation_id)


class AdjustedPriceDatasetStore:
    """Read an immutable canonical-v2 adjusted-price manifest by generation."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        _validate_generation_id(generation_id, "adjusted price")
        candidates = _manifests_under(self.root / "canonical")
        matches: list[Path] = []
        for path in candidates:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                validate_contract("canonical_manifest.v2.json", manifest)
            except (OSError, json.JSONDecodeError, ContractError):
                continue
            if manifest.get("generation_id") == generation_id:
                matches.append(path.parent)
        if not matches:
            raise ContractError(f"unpublished adjusted price dataset: {generation_id}")
        if len(matches) > 1:
            raise ContractError(f"ambiguous adjusted price dataset generation: {generation_id}")
        return self._read_verified_manifest(matches[0])

    def _read_verified_manifest(self, directory: Path) -> dict[str, Any]:
        manifest = _read_json(directory / "manifest.json")
        unsigned = {
            key: value for key, value in manifest.items()
            if key not in {"generation_id", "manifest_digest_sha256", "trust_anchor_sha256"}
        }
        expected_generation, expected_digest = canonical_v2_identities(unsigned)
        if (
            manifest["generation_id"] != expected_generation
            or manifest["manifest_digest_sha256"] != expected_digest
        ):
            raise ContractError("tampered adjusted price manifest identity")
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
        canonical_root = canonical_root.resolve()
        if not canonical_root.is_relative_to(self.root.resolve()):
            raise ContractError("unsafe adjusted price dataset store layout")
        if canonical_root.is_symlink() or any(parent.is_symlink() for parent in canonical_root.parents):
            raise ContractError("unsafe adjusted price dataset root")
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
        frame = pd.read_parquet(data_path)
        if frame.columns.tolist() != manifest["columns"]:
            raise ContractError("label artifact column mismatch")
        if len(frame) != manifest["row_count"]:
            raise ContractError("label artifact row count mismatch")
        actual_dtypes = {key: str(value) for key, value in frame.dtypes.items()}
        expected_dtypes = {key: "datetime64[ns]" if value.startswith("datetime") else value for key, value in manifest["dtypes"].items()}
        if actual_dtypes != expected_dtypes:
            raise ContractError("label artifact dtype mismatch")
        return manifest, frame


class FeaturePreprocessingStore:
    """Manifest-first read boundary for durable feature-preprocessing contracts."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        manifest = _read_manifest(self.root / "feature_preprocessing", generation_id, "feature_preprocessing")
        if manifest.get("quality_report_checksum_sha256") in (None, "0" * 64):
            raise ContractError("feature preprocessing is missing a quality report binding")
        return manifest


class BacktestConfigStore:
    """Read a governed backtest-config manifest without accepting runtime input."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

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
