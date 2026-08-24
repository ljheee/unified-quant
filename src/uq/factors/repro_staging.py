from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow

from ..contracts.canonical_v2 import file_sha256_bytes
from ..errors import ContractError


SERIALIZATION_PROFILE = {
    "profile_id": "parquet-v1",
    "compression": "snappy",
    "dictionary_enabled": True,
    "row_group_size": 64_000,
    "column_order": ["instrument", "datetime", "factor_columns"],
    "sort_order": ["instrument", "datetime"],
    "null_representation": "parquet_null",
}


def environment_profile() -> dict[str, str]:
    return {
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "pandas_version": pd.__version__,
        "pyarrow_version": pyarrow.__version__,
        "numpy_version": np.__version__,
        "blas_backend": "numpy-bundled" if hasattr(np, "__config__") else "unknown",
        "os_family": platform.system(),
        "cpu_architecture": platform.machine(),
        "lockfile_sha256": file_sha256_bytes((Path(__file__).resolve().parents[3] / "uv.lock").read_bytes()),
    }


@dataclass(frozen=True)
class StagingRun:
    directory: Path
    artifact_checksum: str
    logical_fingerprint: str


class ReproStaging:
    def write(self, root: Path, frame: pd.DataFrame, logical_fingerprint: str) -> StagingRun:
        gate_run_id = uuid.uuid4().hex
        directory = root / "repro_staging" / gate_run_id
        if directory.exists():
            raise ContractError("staging run identifier collision")
        staging = directory.with_name(directory.name + ".staging")
        staging.mkdir(parents=True)
        try:
            path = staging / "data.parquet"
            frame.sort_values(SERIALIZATION_PROFILE["sort_order"], kind="mergesort").to_parquet(
                path,
                index=False,
                compression=SERIALIZATION_PROFILE["compression"],
            )
            (staging / "logical_fingerprint").write_text(logical_fingerprint + "\n")
            (staging / "environment.json").write_text(json.dumps(environment_profile(), sort_keys=True, indent=2) + "\n")
            for item in staging.rglob("*"):
                if item.is_file():
                    with item.open("rb") as handle: os.fsync(handle.fileno())
            descriptor = os.open(staging, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            os.replace(staging, directory)
            artifact_path = directory / "data.parquet"
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return StagingRun(directory, file_sha256_bytes(artifact_path.read_bytes()), logical_fingerprint)


def assert_reproducible(left: StagingRun, right: StagingRun) -> None:
    if left.logical_fingerprint != right.logical_fingerprint:
        raise ContractError("logical fingerprint mismatch in reproducibility validation")
    if left.artifact_checksum != right.artifact_checksum:
        raise ContractError("artifact checksum mismatch within locked environment")
