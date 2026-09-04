from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

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
