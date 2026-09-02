from __future__ import annotations

from dataclasses import dataclass
import dataclasses
import hashlib
from datetime import date, datetime
import json
from pathlib import Path
import uuid
import pandas as pd

from ..contracts.config import load_dataset_contract
from ..contracts.schema import Schema, load_schema
from ..errors import CapabilityGapError, ContractError
from ..quality.gate import CrossSourceGate
from ..market.lifecycle import AkShareLifecycleProvider
from ..routing.router import SourceRouter
from ..store.pit_store import CanonicalStore
from ..sources.fetch import FetchResult, FetchStatus


@dataclass(frozen=True)
class IngestReport:
    trade_date: date
    status: str
    rows: int
    source_statuses: dict[str, str]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    published_path: str | None = None
    quality_checksum: str | None = None
    health: dict[str, object] | None = None
    retryable: bool = False
    provenance: dict[str, object] | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "trade_date": self.trade_date.isoformat(),
                "status": self.status,
                "rows": self.rows,
                "source_statuses": self.source_statuses,
                "warnings": list(self.warnings),
                "errors": list(self.errors),
                "published_path": self.published_path,
                "quality_checksum": self.quality_checksum,
                "health": self.health,
                "retryable": self.retryable,
                "provenance": self.provenance,
            },
            sort_keys=True,
        )


def build_tushare_crosscheck_pipeline(
    root: Path,
    project_root: Path | None = None,
    run_health_probe: bool = True,
    require_cross_validation: bool = True,
) -> DailyIngestPipeline:
    """Build TDX-primary with free-Tushare cross-validation and calendar."""
    from ..adapters.mootdx_source import MootdxSourceAdapter
    from ..adapters.tushare_free import TushareFreeAdapter
    from ..market.calendar import TradingCalendar, akshare_calendar
    from ..routing.router import SourceRouter

    project_root = project_root or Path(__file__).resolve().parents[2]
    schema_path = project_root / "config/schemas/bars_daily.research-v1.yaml"
    contract_path = project_root / "config/datasets/bars_daily.research-v1.yaml"
    schema = load_schema(schema_path)
    contract = load_dataset_contract(contract_path, schema)
    tushare = TushareFreeAdapter()

    adapters = {
        "tdx": MootdxSourceAdapter(),
        "tushare": tushare,
    }
    calendar = TradingCalendar(provider=akshare_calendar, provenance="akshare_sina_trade_dates")
    return DailyIngestPipeline(
        root,
        schema_path,
        contract_path,
        SourceRouter(contract, adapters, calendar=calendar),
        require_complete_route=require_cross_validation,
        run_health_probe=run_health_probe,
        require_cross_validation=require_cross_validation,
        verified_rows_only=True,
    )


def build_tdx_pipeline(
    root: Path,
    project_root: Path | None = None,
    run_health_probe: bool = True,
) -> DailyIngestPipeline:
    """Build the current credential-free TDX-first research pipeline."""
    from ..adapters.mootdx_source import MootdxSourceAdapter
    from ..routing.router import SourceRouter

    project_root = project_root or Path(__file__).resolve().parents[2]
    schema_path = project_root / "config/schemas/bars_daily.research-v1.yaml"
    contract_path = project_root / "config/datasets/bars_daily.research-v1.yaml"
    contract = load_dataset_contract(contract_path, load_schema(schema_path))
    adapters = {"tdx": MootdxSourceAdapter()}
    return DailyIngestPipeline(
        root,
        schema_path,
        contract_path,
        SourceRouter(contract, adapters),
        run_health_probe=run_health_probe,
    )


class DailyIngestPipeline:
    def __init__(
        self,
        root: Path,
        schema_path: Path,
        contract_path: Path,
        router: SourceRouter,
        require_complete_route: bool = False,
        run_health_probe: bool = True,
        require_cross_validation: bool = False,
        verified_rows_only: bool | None = None,
        code_version: str = "unified-quant@0.1.0",
    ) -> None:
        self.root = root
        self.schema: Schema = load_schema(schema_path)
        self.contract = load_dataset_contract(contract_path, self.schema)
        self.router = router
        self.require_complete_route = require_complete_route
        self.run_health_probe = run_health_probe
        self.require_cross_validation = require_cross_validation
        policy = self.contract.row_policy or {}
        self.verified_rows_only = (
            bool(policy.get("verified_only", False))
            if verified_rows_only is None
            else verified_rows_only
        )
        self.code_version = code_version
        self.store = CanonicalStore(root)
        self.lifecycle_provider = AkShareLifecycleProvider()

    def _provenance(self, trade_date: date, instruments: list[str]) -> dict[str, object]:
        schema_raw = json.dumps(self.schema.fields, sort_keys=True, default=str)
        contract_raw = json.dumps({
            "required_fields": self.contract.required_fields,
            "primary_source": self.contract.primary_source,
            "cross_validation": self.contract.cross_validation,
            "sources": {name: sorted(capability.provides) for name, capability in self.contract.sources.items()},
        }, sort_keys=True)
        return {
            "code_version": self.code_version,
            "schema_version": self.schema.version,
            "dataset": self.schema.dataset,
            "request": {
                "trade_date": trade_date.isoformat(),
                "instruments": list(instruments),
                "required_fields": list(self.contract.required_fields),
            },
            "calendar": getattr(self.router.calendar, "provenance", None),
            "source_servers": {
                name: getattr(adapter, "server", None)
                for name, adapter in self.router.adapters.items()
            },
            "fingerprints": {
                "schema_sha256": hashlib.sha256(schema_raw.encode()).hexdigest(),
                "contract_sha256": hashlib.sha256(contract_raw.encode()).hexdigest(),
            },
        }

    def _health(self) -> dict[str, object]:
        primary = self.router.adapters.get(self.contract.primary_source)
        probe = getattr(primary, "health_probe", None)
        if probe is None:
            return {"ok": None, "reason": "adapter does not support health probes"}
        try:
            return probe()
        except Exception as exc:
            return {"ok": False, "error": f"health probe failed: {exc}"}

    def run(self, trade_date: date, instruments: list[str]) -> IngestReport:
        try:
            return self._run(trade_date, instruments)
        except CapabilityGapError as exc:
            return IngestReport(trade_date, "routing_failure", 0, {}, (), (str(exc),))
        except ContractError as exc:
            status = str(getattr(exc, "status", "upstream_error"))
            return IngestReport(
                trade_date,
                f"source_{status}",
                0,
                {},
                (),
                (str(exc),),
                retryable=bool(getattr(exc, "retryable", False)),
            )
        except (OSError, RuntimeError) as exc:
            status = "source_failure" if "mootdx" in str(exc).lower() or "client unavailable" in str(exc).lower() else "storage_failure"
            return IngestReport(trade_date, status, 0, {}, (), (str(exc),))

    def _run(self, trade_date: date, instruments: list[str]) -> IngestReport:
        requested_fields = list(self.contract.required_fields)
        provenance = self._provenance(trade_date, instruments)
        raw_dir = self.root / "raw" / trade_date.isoformat()
        health = self._health() if self.run_health_probe else {"ok": None, "skipped": True}
        if self.run_health_probe and health.get("ok") is not True:
            reason = health.get("error") or "probe unavailable"
            return IngestReport(
                trade_date,
                "primary_source_unhealthy",
                0,
                {self.contract.primary_source: "unverified"},
                (),
            (f"primary source health failed: {reason}",),
            health=health,
            provenance=provenance,
            )
        route = self.router.fetch(
            instruments=instruments,
            start=trade_date.isoformat(),
            end=trade_date.isoformat(),
            fields=requested_fields,
        )
        raw_capture = bool((self.contract.row_policy or {}).get("raw_capture", True))
        source_results: dict[str, FetchResult] = dict(route.results)
        frames: dict[str, pd.DataFrame] = {}
        warnings: list[str] = []
        errors: list[str] = []
        for source_name, result in source_results.items():
            warnings.extend(source_results[source_name].warnings)
            errors.extend(source_results[source_name].errors)
            payload = result.metadata.get("raw_payload")
            if isinstance(payload, bytes) and raw_capture:
                path = raw_dir / f"{source_name}-{uuid.uuid4().hex}.bin"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                source_results[source_name] = dataclasses.replace(
                    result,
                    raw_artifact_ref=str(path.relative_to(self.root)),
                    raw_checksum_sha256=hashlib.sha256(payload).hexdigest(),
                )

        primary_result = source_results.get(self.contract.primary_source)
        if primary_result is None or primary_result.status != FetchStatus.SUCCESS:
            status = primary_result.status.value if primary_result else "unavailable"
            return IngestReport(trade_date, f"primary_source_{status}", 0, {k: v.status.value for k, v in source_results.items()}, tuple(warnings), tuple(errors) or (f"primary source unusable: {status}",))

        if requested_fields and set(requested_fields) - primary_result.delivered_fields:
            errors.append(f"primary source delivered missing fields: {sorted(set(requested_fields) - primary_result.delivered_fields)}")
            return IngestReport(trade_date, "rejected", len(primary_result.rows), {k: v.status.value for k, v in source_results.items()}, tuple(warnings), tuple(errors))

        if self.require_complete_route and not route.is_complete:
            status = "coverage_unverified" if route.coverage_status == "unverified" else "rejected"
            errors.append(f"route coverage is {route.coverage_status}; publication requires complete")
            return IngestReport(trade_date, status, 0, {k: v.status.value for k, v in source_results.items()}, tuple(warnings), tuple(errors))

        if route.coverage_status != "complete":
            warnings.append(f"route coverage is {route.coverage_status}; output remains exploratory")

        frames[self.contract.primary_source] = primary_result.rows
        for source_name, result in source_results.items():
            if source_name != self.contract.primary_source and result.is_usable and not result.rows.empty:
                frames[source_name] = result.rows

        if self.require_cross_validation:
            secondary_sources = {
                str(rule.get("compare_with"))
                for rule in self.contract.cross_validation.values()
            }
            missing_secondary = sorted(secondary_sources - set(frames))
            if missing_secondary:
                errors.append(f"required cross-validation source absent: {missing_secondary}")
                return IngestReport(trade_date, "cross_validation_missing", len(primary_result.rows), {k: v.status.value for k, v in source_results.items()}, tuple(warnings), tuple(errors))

        if self.verified_rows_only and instruments:
            primary_keys = set(map(tuple, primary_result.rows[["instrument", "datetime"]].itertuples(index=False, name=None)))
            expected_keys = {
                (instrument, pd.Timestamp(trade_date))
                for instrument in instruments
            }
            missing_keys = expected_keys - primary_keys
            lifecycle = self.lifecycle_provider.classify_missing(
                trade_date,
                sorted({instrument for instrument, _ in missing_keys}),
                sources=self.contract.row_policy.get("lifecycle_auxiliary_sources", ()),
            )
            lifecycle.update(self.lifecycle_provider.suspension_window(
                trade_date,
                sorted({instrument for instrument, _ in missing_keys}),
                sources=self.contract.row_policy.get("lifecycle_auxiliary_sources", ()),
            ))
            explained_missing = {
                key for key in missing_keys
                if lifecycle[key[0]] in self.lifecycle_provider.STATUS_EXPECTED_MISSING
            }
            unexplained = missing_keys - explained_missing
            allow_unknown = bool(self.contract.row_policy.get("allow_unknown_missing", False))
            if unexplained and not allow_unknown:
                return IngestReport(
                    trade_date,
                    "missing_verified_rows",
                    len(primary_result.rows),
                    {k: v.status.value for k, v in source_results.items()},
                    tuple(warnings),
                    (
                        f"verified-only row policy rejects unexplained missing keys: "
                        f"{sorted(f'{key[0]}@{key[1].date()}' for key in unexplained)}",
                    ),
                    provenance=provenance,
                )
            if unexplained:
                warnings.append(
                    "verified-only row policy allowed unknown missing keys by configuration: "
                    + ",".join(sorted(f"{key[0]}@{key[1].date()}" for key in unexplained))
                )
            if explained_missing:
                warnings.append(
                    "verified-only row policy classified expected missing keys as not listed: "
                    + ",".join(sorted(f"{key[0]}@{key[1].date()}" for key in explained_missing))
                )

        gate = CrossSourceGate(
            self.contract.primary_source,
            self.contract.cross_validation,
            self.contract.owners,
        )
        quality = gate.merge(frames, ["instrument", "datetime"], set(requested_fields))
        if quality.frame.empty and instruments:
            quality = dataclasses.replace(
                quality,
                accepted=False,
                errors=list(quality.errors) + ["expected rows for configured instruments but received an empty partition"],
            )
        errors.extend(quality.errors)
        if not quality.accepted:
            return IngestReport(trade_date, "rejected", 0, {k: v.status.value for k, v in source_results.items()}, tuple(warnings), tuple(errors))

        try:
            path: Path | None = None
            path = self.store.publish(
                schema=self.schema,
                partition_date=trade_date,
                frame=quality.frame,
                lineage={name: vars(item) for name, item in quality.lineage.items()},
                source_versions={name: result.metadata.get("provider", name) if isinstance(result.metadata, dict) else name for name, result in source_results.items()},
                quality_checksum=quality.checksum,
                raw_artifacts={
                    name: {
                        "artifact_ref": result.raw_artifact_ref,
                        "checksum_sha256": result.raw_checksum_sha256,
                    }
                    for name, result in source_results.items()
                    if result.raw_artifact_ref
                },
            )
        except ContractError as exc:
            return IngestReport(trade_date, "publication_conflict", len(quality.frame), {k: v.status.value for k, v in source_results.items()}, tuple(warnings), (str(exc),), str(path) if path else None)
        except OSError as exc:
            return IngestReport(trade_date, "storage_failure", len(quality.frame), {k: v.status.value for k, v in source_results.items()}, tuple(warnings), (f"publication I/O failed: {exc}",), str(path) if path else None)

        return IngestReport(
            trade_date,
            "published",
            len(quality.frame),
            {name: result.status.value for name, result in source_results.items()},
            tuple(warnings),
            (),
            str(path),
            quality.checksum,
            health,
            False,
            provenance,
        )
