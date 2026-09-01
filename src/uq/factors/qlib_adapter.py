"""Qlib factor adapter: compute reviewed Alpha158 factors in an isolated process."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.factor_governance import FactorRegistry
from ..errors import ContractError

_QLIB_INPUT_COLUMNS = ("open", "high", "low", "close", "volume")
_ALLOWED_QLIB_FIELDS = {"$" + column for column in _QLIB_INPUT_COLUMNS}
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFINITIONS_ROOT = _REPO_ROOT / "config" / "factor-sets"


class QlibNotInstalledError(ContractError):
    """Raised when the reviewed pyqlib runtime is not available."""


_QLIB_COMPUTE_TIMEOUT_SECONDS = 600
_INSTRUMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _validate_worker_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ContractError("malformed qlib worker payload")
    required = {
        "provider_uri", "instruments", "expressions", "factor_names",
        "start_date", "end_date", "result_path",
    }
    if not required.issubset(payload):
        raise ContractError("qlib worker payload is missing required fields")
    for key in ("provider_uri", "start_date", "end_date", "result_path"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ContractError(f"invalid qlib worker payload field: {key}")
    for key in ("instruments", "expressions", "factor_names"):
        if not isinstance(payload[key], list) or not payload[key] or not all(
            isinstance(item, str) for item in payload[key]
        ):
            raise ContractError(f"invalid qlib worker payload field: {key}")


def _import_qlib():
    try:
        import qlib
        return qlib
    except ImportError as exc:
        raise QlibNotInstalledError(
            "pyqlib==0.9.7 is required for qlib factor computation"
        ) from exc


def _qlib_worker(payload_path: str) -> int:
    """Child-process entry point. Qlib global state never escapes this process."""
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    _validate_worker_payload(payload)
    qlib = _import_qlib()
    qlib.init(
        provider_uri=payload["provider_uri"],
        region="cn",
        kernels=1,
        joblib_backend="threading",
    )
    expressions = payload["expressions"]
    result = qlib.data.D.features(
        payload["instruments"],
        expressions,
        start_time=payload["start_date"],
        end_time=payload["end_date"],
    )
    result.columns = payload["factor_names"]
    result = result.reset_index()
    result["datetime"] = pd.to_datetime(result["datetime"]).dt.strftime("%Y-%m-%d")
    result["instrument"] = result["instrument"].str.upper()
    output = result[["instrument", "datetime", *payload["factor_names"]]]
    output.sort_values(["instrument", "datetime"], kind="mergesort").to_parquet(
        payload["result_path"], index=False
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_qlib_worker(sys.argv[1]))


class QlibFactorAdapter:
    """Compute a registry-reviewed Qlib factor set without mutating process state."""

    def __init__(self, definition_path: Path | str) -> None:
        path = Path(definition_path)
        if path.is_symlink() or not path.is_file():
            raise ContractError("qlib factor definition must be a regular file")
        resolved = path.resolve(strict=True)
        if resolved.parent != _DEFINITIONS_ROOT.resolve():
            raise ContractError("qlib factor definition is outside the reviewed registry")
        raw = resolved.read_bytes()
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("malformed qlib factor definition") from exc
        registry = FactorRegistry(_REPO_ROOT)
        registered = registry.get(document.get("factor_set"), document.get("factor_version"))
        if document != registered.document:
            raise ContractError("qlib factor definition does not match reviewed registry")
        if registered.document.get("set_definition_version") != 2:
            raise ContractError("qlib factor definition schema must be v2")
        if registered.document.get("status") != "reviewed":
            raise ContractError("factor set definition must be reviewed")
        self.definition = registered.document
        self.definition_checksum_sha256 = file_sha256_bytes(raw)
        self.definition_path = resolved
        self._validate_document(registered.document)

    @property
    def factor_set(self) -> str:
        return self.definition["factor_set"]

    @property
    def factor_version(self) -> str:
        return self.definition["factor_version"]

    @property
    def factor_names(self) -> list[str]:
        return sorted(factor["name"] for factor in self.definition["factors"])

    @property
    def qlib_expressions(self) -> dict[str, str]:
        return {factor["name"]: factor["expression"] for factor in self.definition["factors"]}

    def compute(
        self,
        panel: pd.DataFrame,
        *,
        instruments: list[str],
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        qlib = _import_qlib()
        self._validate_engine_version()
        self._validate_request(panel, instruments, start_date, end_date)
        normalized_start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        normalized_end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory(prefix="uq_qlib_") as temporary_root:
            root = Path(temporary_root)
            qlib_dir = root / "qlib_data"
            panel_path = root / "panel.parquet"
            result_path = root / "result.parquet"
            payload_path = root / "payload.json"
            panel.to_parquet(panel_path, index=False)
            self._write_qlib_data(panel, instruments, qlib_dir)
            payload = {
                "provider_uri": str(qlib_dir),
                "instruments": [instrument.lower() for instrument in instruments],
                "expressions": [self.qlib_expressions[name] for name in self.factor_names],
                "factor_names": self.factor_names,
                "start_date": normalized_start,
                "end_date": normalized_end,
                "panel_path": str(panel_path),
                "result_path": str(result_path),
            }
            payload_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "uq.factors.qlib_adapter", str(payload_path)],
                capture_output=True,
                text=True,
                timeout=_QLIB_COMPUTE_TIMEOUT_SECONDS,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ContractError(f"qlib computation failed: {detail}")
            if not result_path.is_file():
                raise ContractError("qlib computation did not produce a result")
            result = pd.read_parquet(result_path)
            self._reconcile_result(result, panel, instruments, normalized_start, normalized_end)
            return result

    def partition_frames(self, result: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
        partitions: list[tuple[str, pd.DataFrame]] = []
        for date_value, frame in result.groupby("datetime", sort=True):
            partitions.append((str(date_value), frame.reset_index(drop=True)))
        return partitions

    def _validate_request(
        self,
        panel: pd.DataFrame,
        instruments: list[str],
        start_date: str,
        end_date: str,
    ) -> None:
        if len(set(instruments)) != len(instruments):
            raise ContractError("duplicate requested qlib instruments")
        if any(not isinstance(instrument, str) or not _INSTRUMENT_PATTERN.fullmatch(instrument) or instrument in {".", ".."} for instrument in instruments):
            raise ContractError("unsafe qlib instrument identifier")
        required_columns = {"instrument", "datetime", *_QLIB_INPUT_COLUMNS}
        missing_columns = sorted(required_columns - set(panel.columns))
        if missing_columns:
            raise ContractError(f"missing qlib input columns: {missing_columns}")
        if panel[list(required_columns)].isna().any().any():
            raise ContractError("qlib input panel contains null required values")
        numeric = panel[list(_QLIB_INPUT_COLUMNS)].apply(pd.api.types.is_numeric_dtype)
        if not numeric.all():
            raise ContractError("qlib input price columns must be numeric")
        if np.isinf(panel[list(_QLIB_INPUT_COLUMNS)].to_numpy(dtype=float)).any():
            raise ContractError("qlib input prices contain non-finite values")
        if panel.duplicated(["instrument", "datetime"]).any():
            raise ContractError("duplicate qlib input panel keys")
        panel_instruments = sorted(panel["instrument"].astype(str).unique())
        if panel_instruments != sorted(set(instruments)):
            raise ContractError("panel instruments do not match requested instrument list")
        dates = pd.to_datetime(panel["datetime"], errors="raise")
        if dates.isna().any():
            raise ContractError("qlib input panel contains invalid dates")
        qlib_dates = pd.DatetimeIndex(sorted(dates.unique()))
        if not len(qlib_dates):
            raise ContractError("cannot compute qlib factors from an empty panel")
        if len(panel) != len(instruments) * len(qlib_dates):
            raise ContractError("qlib input panel is not a complete instrument/date panel")
        maximum_history = max(int(factor["minimum_history"]) for factor in self.definition["factors"])
        if len(qlib_dates) < maximum_history:
            raise ContractError(
                f"qlib input history is shorter than reviewed minimum_history={maximum_history}"
            )
        requested_start = pd.Timestamp(start_date)
        requested_end = pd.Timestamp(end_date)
        if requested_start < qlib_dates[0] or requested_end > qlib_dates[-1]:
            raise ContractError("qlib computation window extends beyond governed input panel")

    def _write_qlib_data(
        self,
        panel: pd.DataFrame,
        instruments: list[str],
        qlib_dir: Path,
    ) -> None:
        qlib_dates = pd.DatetimeIndex(sorted(pd.to_datetime(panel["datetime"]).unique()))
        cal_dir = qlib_dir / "calendars"
        cal_dir.mkdir(parents=True)
        with (cal_dir / "day.txt").open("w", encoding="utf-8") as handle:
            for date_value in qlib_dates:
                handle.write(f"{date_value.strftime('%Y-%m-%d')}\n")

        inst_dir = qlib_dir / "instruments"
        inst_dir.mkdir(parents=True)
        with (inst_dir / "all.txt").open("w", encoding="utf-8") as handle:
            for instrument in instruments:
                handle.write(
                    f"{instrument.lower()}\t{qlib_dates[0].strftime('%Y-%m-%d')}\t"
                    f"{qlib_dates[-1].strftime('%Y-%m-%d')}\n"
                )

        for instrument in instruments:
            feature_dir = qlib_dir / "features" / instrument.lower()
            feature_dir.mkdir(parents=True)
            instrument_data = panel[panel["instrument"] == instrument].copy()
            instrument_data["datetime"] = pd.to_datetime(instrument_data["datetime"])
            instrument_data = instrument_data.set_index("datetime")
            for column in _QLIB_INPUT_COLUMNS:
                values = instrument_data[column].reindex(qlib_dates).to_numpy(dtype=np.float32)
                artifact = np.hstack([[0], values]).astype("<f")
                artifact.tofile(str(feature_dir / f"{column}.day.bin"))

    def _reconcile_result(
        self,
        result: pd.DataFrame,
        panel: pd.DataFrame,
        instruments: list[str],
        start_date: str,
        end_date: str,
    ) -> None:
        expected_columns = ["instrument", "datetime", *self.factor_names]
        if list(result.columns) != expected_columns:
            raise ContractError("qlib result columns do not match reviewed factor set")
        expected_dates = pd.to_datetime(panel["datetime"]).dt.strftime("%Y-%m-%d")
        expected_keys = {
            (instrument, date_value)
            for instrument in instruments
            for date_value in sorted(expected_dates.unique())
            if start_date <= date_value <= end_date
        }
        actual_keys = set(map(tuple, result[["instrument", "datetime"]].to_numpy()))
        if actual_keys != expected_keys or len(result) != len(expected_keys):
            raise ContractError("qlib result keys do not match governed input panel")
        if result.duplicated(["instrument", "datetime"]).any():
            raise ContractError("duplicate qlib result keys")
        factor_columns = self.factor_names
        if np.isinf(result[factor_columns].to_numpy(dtype=float)).any():
            raise ContractError("qlib result contains non-finite values")

    def _validate_engine_version(self) -> None:
        installed = str(_import_qlib().__version__)
        reviewed = self.definition.get("reviewed_engine_versions", [])
        if installed not in reviewed:
            raise ContractError(
                f"qlib version {installed} is not reviewed for {self.factor_set}/{self.factor_version}"
            )

    @staticmethod
    def _validate_document(document: dict) -> None:
        if document.get("status") != "reviewed":
            raise ContractError("factor set definition must be reviewed")
        factors = document.get("factors", [])
        if not factors:
            raise ContractError("qlib factor set has no reviewed factors")
        if document.get("qlib_expression_set") != "Alpha158":
            raise ContractError("only the reviewed Alpha158 factor set is computable")
        if not document.get("reviewed_engine_versions"):
            raise ContractError("reviewed qlib engine versions are required")
        if not document.get("input_bindings"):
            raise ContractError("reviewed qlib input bindings are required")

        names = [factor.get("name") for factor in factors]
        qlib_names = [factor.get("qlib_name") for factor in factors]
        if len(names) != len(set(names)) or len(qlib_names) != len(set(qlib_names)):
            raise ContractError("qlib factor mapping must be unique")
        if names != [f"qlib_{name.lower()}" for name in qlib_names]:
            raise ContractError("qlib factor names do not follow the reviewed mapping")
        field_pattern = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
        ref_pattern = re.compile(
            r"Ref\s*\(\s*\$[A-Za-z_][A-Za-z0-9_]*\s*,\s*([+-]?\s*\d+)\s*\)"
        )
        for factor in factors:
            expression = factor.get("expression", "")
            fields = set(field_pattern.findall(expression))
            if not fields <= _ALLOWED_QLIB_FIELDS:
                raise ContractError(
                    f"qlib factor {factor.get('qlib_name')} uses ungoverned fields: "
                    f"{sorted(fields - _ALLOWED_QLIB_FIELDS)}"
                )
            for value in ref_pattern.findall(expression):
                if int(value.replace(" ", "")) < 0:
                    raise ContractError(
                        f"forward-looking qlib expression: {factor.get('qlib_name')}"
                    )

    @staticmethod
    def _fingerprint() -> str:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    @staticmethod
    def qlib_version() -> str:
        return str(_import_qlib().__version__)
