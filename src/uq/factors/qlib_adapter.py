"""Qlib factor adapter: compute Alpha158 factors via Qlib expression engine."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from ..errors import ContractError


class QlibNotInstalledError(ContractError):
    """Raised when pyqlib is not available in the environment."""


def _import_qlib():
    try:
        import qlib
        return qlib
    except ImportError as exc:
        raise QlibNotInstalledError(
            "pyqlib is not installed. Install with: pip install pyqlib>=0.9.7"
        ) from exc


class QlibFactorAdapter:
    """Compute Alpha158 factors using Qlib's expression engine.

    Writes UQ governed data to a temporary Qlib-format directory,
    initializes Qlib, evaluates expressions, then cleans up.
    """

    def __init__(self, definition_path: Path | str) -> None:
        self.definition = json.loads(Path(definition_path).read_text())
        if self.definition.get("status") != "reviewed":
            raise ContractError("factor set definition must be reviewed")
        self._validate_lookahead()

    @property
    def factor_set(self) -> str:
        return self.definition["factor_set"]

    @property
    def factor_version(self) -> str:
        return self.definition["factor_version"]

    @property
    def factor_names(self) -> list[str]:
        return sorted(f["name"] for f in self.definition["factors"])

    @property
    def qlib_expressions(self) -> dict[str, str]:
        """Map UQ factor name -> Qlib expression string."""
        return {f["name"]: f["expression"] for f in self.definition["factors"]}

    def compute(
        self,
        panel: pd.DataFrame,
        *,
        instruments: list[str],
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Compute all factors in this set from a governed price panel.

        Args:
            panel: DataFrame with columns [instrument, datetime, open, high,
                   low, close, volume]. Multi-date, multi-instrument.
            instruments: List of instrument identifiers.
            start_date: Start of computation window.
            end_date: End of computation window.

        Returns:
            DataFrame with columns [instrument, datetime, <factor_names>].
        """
        qlib = _import_qlib()
        if len(set(instruments)) != len(instruments):
            raise ContractError("duplicate requested qlib instruments")
        panel_instruments = sorted(panel["instrument"].astype(str).unique())
        if panel_instruments != sorted(set(instruments)):
            raise ContractError("panel instruments do not match requested instrument list")
        qlib_dir = self._write_qlib_data(panel, instruments)
        previous_provider = None
        try:
            import qlib.config

            if qlib.config.C.registered:
                previous_provider = next(iter(qlib.config.C.dpm.provider_uri.values()), None)
            self._init_qlib(str(qlib_dir))
            expressions = list(self.qlib_expressions.values())
            names = list(self.qlib_expressions.keys())
            result = qlib.data.D.features(
                [inst.lower() for inst in instruments],
                expressions,
                start_time=start_date,
                end_time=end_date,
            )
            result.columns = names
            result = result.reset_index()
            result["datetime"] = pd.to_datetime(result["datetime"]).dt.strftime("%Y-%m-%d")
            result["instrument"] = result["instrument"].str.upper()
            return result[["instrument", "datetime"] + sorted(names)]
        finally:
            if previous_provider is not None:
                self._init_qlib(str(previous_provider))
            shutil.rmtree(qlib_dir, ignore_errors=True)

    def partition_frames(self, result: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
        """Slice a full-range result into deterministic per-date frames."""
        partitions: list[tuple[str, pd.DataFrame]] = []
        for date_value, frame in result.groupby("datetime", sort=True):
            partitions.append((str(date_value), frame.reset_index(drop=True)))
        return partitions

    def _write_qlib_data(self, panel: pd.DataFrame, instruments: list[str]) -> Path:
        """Write UQ canonical panel to temporary Qlib .bin format."""
        tmpdir = Path(tempfile.mkdtemp(prefix="uq_qlib_"))
        qlib_dir = tmpdir / "qlib_data"

        qlib_dates = pd.DatetimeIndex(sorted(panel["datetime"].unique()))
        if not len(qlib_dates):
            raise ContractError("cannot compute qlib factors from an empty panel")
        if panel.duplicated(["instrument", "datetime"]).any():
            raise ContractError("duplicate qlib input panel keys")
        required_columns = {"instrument", "datetime", "open", "high", "low", "close", "volume"}
        missing_columns = sorted(required_columns - set(panel.columns))
        if missing_columns:
            raise ContractError(f"missing qlib input columns: {missing_columns}")

        cal_dir = qlib_dir / "calendars"
        cal_dir.mkdir(parents=True, exist_ok=True)
        with open(cal_dir / "day.txt", "w") as f:
            for d in qlib_dates:
                f.write(f"{d.strftime('%Y-%m-%d')}\n")

        inst_dir = qlib_dir / "instruments"
        inst_dir.mkdir(parents=True, exist_ok=True)
        with open(inst_dir / "all.txt", "w") as f:
            for inst in instruments:
                f.write(f"{inst.lower()}\t{qlib_dates[0].strftime('%Y-%m-%d')}\t"
                        f"{qlib_dates[-1].strftime('%Y-%m-%d')}\n")

        columns = ["open", "high", "low", "close", "volume"]
        for inst in instruments:
            feat_dir = qlib_dir / "features" / inst.lower()
            feat_dir.mkdir(parents=True, exist_ok=True)
            inst_data = panel[panel["instrument"] == inst].set_index("datetime")
            for col in columns:
                if col not in inst_data.columns:
                    continue
                values = inst_data[col].reindex(qlib_dates).values.astype(np.float32)
                data = np.hstack([[0], values]).astype("<f")
                data.tofile(str(feat_dir / f"{col}.day.bin"))

        return qlib_dir

    def _init_qlib(self, provider_uri: str) -> None:
        qlib = _import_qlib()
        qlib.init(
            provider_uri=provider_uri,
            region="cn",
            kernels=1,
            joblib_backend="threading",
        )

    def _validate_lookahead(self) -> None:
        pattern = re.compile(r"Ref\s*\(\s*\$[A-Za-z_][A-Za-z0-9_]*\s*,\s*([+-]?\d+)\s*\)")
        for factor in self.definition["factors"]:
            for value in pattern.findall(factor["expression"]):
                if int(value) < 0:
                    raise ContractError(f"forward-looking qlib expression: {factor['qlib_name']}")

    @staticmethod
    def _fingerprint() -> str:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    @staticmethod
    def qlib_version() -> str:
        return str(_import_qlib().__version__)
