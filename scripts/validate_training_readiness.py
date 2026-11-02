"""Fail-closed pre-training readiness check over governed inputs (pendding item 5).

Reads PIT-visible membership from an IndexMembershipStore and canonical
bars_daily partitions through the manifest-first reader, then evaluates the
cross-input conditions a training run silently depends on. Exit code 0 means the
window is trainable; 1 means at least one rule blocked it.

By default the trading calendar still comes from the primary bars vendor
(Tushare ``trade_cal``), so ``bar_session_gap`` proves internal consistency.
Pass ``--independent-calendar`` to additionally verify every session against an
EastMoney-derived index session list; the run is rejected if that source is
unreachable, stale, or the two calendars disagree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.adapters.tushare_free import _to_instrument
from uq.contracts.index_membership import IndexMembershipStore
from uq.contracts.gate_contracts import sha256_json, validate_contract
from uq.contracts.schema import load_schema
from uq.contracts.tushare_dependency import TushareDependencyStore
from uq.errors import ContractError
from uq.pipeline.readiness import assess_readiness
from uq.pipeline.readiness import sessions_by_instrument
from uq.store.reader import ManifestFirstReader


def _members_by_session(
    store: IndexMembershipStore,
    universe_id: str,
    sessions: list[dt.date],
    notes: dict[str, Any],
) -> dict[dt.date, list[str]]:
    visible: dict[dt.date, list[str]] = {}
    unavailable: list[str] = []
    for session in sessions:
        try:
            _, members = store.as_of(universe_id, session)
        except (ContractError, OSError, ValueError) as exc:
            unavailable.append(f"{session.isoformat()}: {exc}")
            continue
        visible[session] = sorted(str(value) for value in members["instrument"])
    if unavailable:
        notes["membership_unavailable_sessions"] = unavailable
    return visible


def _bars_for_sessions(
    reader: ManifestFirstReader,
    schema,
    sessions: list[dt.date],
    notes: dict[str, Any],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    missing: list[str] = []
    for session in sessions:
        try:
            frames.append(reader.read(schema, session))
        except (ContractError, OSError, ValueError) as exc:
            missing.append(f"{session.isoformat()}: {exc}")
    if missing:
        notes["bar_partitions_unreadable"] = missing
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _suspensions_for_gaps(
    adapter: Tushare2kAdapter,
    sessions: list[dt.date],
    members_by_session: dict[dt.date, list[str]],
    bars: pd.DataFrame,
    notes: dict[str, Any],
    *,
    request_interval_seconds: float = 0.5,
) -> dict[str, list[dt.date]]:
    """Query suspend_d for the sessions where at least one instrument lacks bars.

    A suspension only matters on a session some instrument is missing, so the
    gate pays one whole-market call per candidate session instead of one per
    window session. Consequence: ``suspension_bar_conflict`` can only fire on
    the sessions listed in the report notes.
    """
    present = sessions_by_instrument(bars)
    universe = {name for values in members_by_session.values() for name in values} | set(present)
    gap_days = [
        day
        for day in sessions
        if universe - {name for name, values in present.items() if day in values}
    ]
    if not gap_days:
        notes["suspension_provenance"] = "tushare:suspend_d(not-required)"
        notes["suspension_sessions"] = []
        return {}
    suspensions: dict[str, list[dt.date]] = {}
    for index, day in enumerate(gap_days):
        if index and request_interval_seconds:
            time.sleep(request_interval_seconds)
        frame = adapter.suspension(trade_date=day.strftime("%Y%m%d"))
        if frame.empty:
            continue
        for row in frame["ts_code"].astype(str):
            try:
                instrument = _to_instrument(row)
            except ValueError as exc:
                notes.setdefault("suspension_unmapped_codes", []).append(f"{row}: {exc}")
                continue
            suspensions.setdefault(instrument, []).append(day)
    notes["suspension_provenance"] = f"tushare:suspend_d({len(gap_days)} sessions)"
    notes["suspension_sessions"] = [day.isoformat() for day in gap_days]
    return suspensions


def _read_factor_partition_set(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable factor partition set {path}: {exc}") from exc


def factor_sessions_from_document(document: Any) -> list[dt.date]:
    """Session dates a declared factor partition set covers.

    Accepts the ``factor_partition_set`` binding document produced by
    ``uq.factors.partition_set.build_factor_partition_set`` (``partitions`` of
    ``partition_date``) or a bare list of ISO dates, so a caller that has not
    built the binding yet can still assert the linkage.
    """
    raw: Any = None
    if isinstance(document, dict):
        raw = document.get("partitions", document.get("factor_sessions"))
    elif isinstance(document, list):
        raw = document
    if not isinstance(raw, list) or not raw:
        raise SystemExit(
            "factor partition set must be a non-empty list or an object with 'partitions'/'factor_sessions'"
        )
    sessions: list[dt.date] = []
    for item in raw:
        value = item.get("partition_date") if isinstance(item, dict) else item
        if not isinstance(value, str):
            raise SystemExit(f"factor partition entry has no date: {item!r}")
        try:
            sessions.append(dt.date.fromisoformat(value))
        except ValueError as exc:
            raise SystemExit(f"factor partition date is not ISO: {value!r}") from exc
    return sorted(set(sessions))


def delistings_from_snapshot(
    manifest: dict[str, Any], frame: pd.DataFrame
) -> tuple[set[str], dict[str, dt.date]]:
    """Normalize an immutable D-status stock_basic response for readiness.

    The current status roster is used only as lifecycle evidence. A historical
    gap is excused by ``delist_date`` on and after its effective date; gaps
    strictly before the effective date remain blocking.
    """
    query = manifest.get("query")
    if not isinstance(query, dict) or query.get("list_status") != "D":
        raise ContractError("delisting snapshot must be scoped to stock_basic list_status=D")
    requested_fields = {
        value.strip() for value in str(query.get("fields", "")).split(",") if value.strip()
    }
    if not {"ts_code", "list_status", "delist_date"} <= requested_fields:
        raise ContractError("delisting snapshot must request ts_code,list_status,delist_date")
    if not {"ts_code", "list_status", "delist_date"} <= set(frame.columns):
        raise ContractError("delisting snapshot lacks ts_code, list_status, or delist_date")

    delisted: set[str] = set()
    delist_dates: dict[str, dt.date] = {}
    for row in frame[["ts_code", "list_status", "delist_date"]].itertuples(index=False):
        code, status, raw_date = (str(row[0]).strip(), str(row[1]).strip(), row[2])
        if status != "D":
            raise ContractError("delisting snapshot contains a row outside list_status=D")
        try:
            instrument = _to_instrument(code)
        except ValueError:
            # Non-A-share rows cannot map into this readiness universe.
            continue
        delisted.add(instrument)
        if pd.isna(raw_date) or not str(raw_date).strip():
            continue
        raw_text = str(raw_date).strip()
        try:
            parsed = (
                dt.datetime.strptime(raw_text, "%Y%m%d").date()
                if len(raw_text) == 8
                else dt.date.fromisoformat(raw_text)
            )
        except ValueError as exc:
            raise ContractError(f"invalid delist_date for {instrument}: {raw_text!r}") from exc
        prior = delist_dates.get(instrument)
        if prior is not None and prior != parsed:
            raise ContractError(f"conflicting delist_date rows for {instrument}")
        delist_dates[instrument] = parsed
    return delisted, delist_dates


def _source_trade_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    try:
        return dt.datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 else dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"invalid suspend_d trade_date: {text!r}") from exc


def suspensions_from_snapshots(
    root: Path, sessions: list[dt.date], notes: dict[str, Any]
) -> dict[str, list[dt.date]]:
    """Read exact daily suspend_d(S) responses from immutable source snapshots."""
    store = TushareDependencyStore(root)
    manifests = store.list_manifests("suspend_d")
    by_query: dict[str, dict[str, Any]] = {}
    for manifest in manifests:
        key = json.dumps(manifest["query"], sort_keys=True, separators=(",", ":"))
        prior = by_query.get(key)
        if prior is None or manifest["source_fetched_at"] > prior["source_fetched_at"]:
            by_query[key] = manifest

    suspensions: dict[str, list[dt.date]] = {}
    snapshot_bindings: list[dict[str, str]] = []
    for session in sessions:
        query = {"trade_date": session.strftime("%Y%m%d"), "suspend_type": "S"}
        key = json.dumps(query, sort_keys=True, separators=(",", ":"))
        manifest = by_query.get(key)
        if manifest is None:
            raise ContractError(f"missing suspend_d(S) snapshot for {session.isoformat()}")
        verified_manifest, frame = store.read_frame(manifest["generation_id"], dataset="suspend_d")
        if not {"ts_code", "trade_date"} <= set(frame.columns):
            raise ContractError(f"suspend_d snapshot lacks required columns for {session.isoformat()}")
        if not frame.empty:
            observed = {_source_trade_date(value) for value in frame["trade_date"]}
            if observed != {session}:
                raise ContractError(f"suspend_d snapshot rows do not match {session.isoformat()}")
            if "suspend_type" in frame.columns and set(frame["suspend_type"].astype(str)) != {"S"}:
                raise ContractError(f"suspend_d snapshot contains non-suspension rows for {session.isoformat()}")
            for code in frame["ts_code"].astype(str):
                try:
                    instrument = _to_instrument(code)
                except ValueError as exc:
                    notes.setdefault("suspension_unmapped_codes", []).append(f"{session}: {code}")
                    continue
                suspensions.setdefault(instrument, []).append(session)
        snapshot_bindings.append({
            "trade_date": session.isoformat(),
            "generation_id": verified_manifest["generation_id"],
            "data_checksum_sha256": verified_manifest["data_artifact"]["checksum_sha256"],
        })
    digest = sha256_json(snapshot_bindings)
    notes["suspension_provenance"] = f"tushare_dependency:suspend_d(S):{digest}"
    notes["suspension_snapshot_count"] = len(snapshot_bindings)
    notes["suspension_snapshot_set_sha256"] = digest
    return suspensions


def suspension_intervals_from_document(
    path: Path, sessions: list[dt.date], notes: dict[str, Any]
) -> dict[str, list[dt.date]]:
    """Expand source-cited issuer/exchange suspension intervals over sessions.

    ``suspend_d`` snapshots can omit dates inside a long halt. This supplemental
    evidence format records the cited interval without inventing daily provider
    rows; only declared trading sessions in the inclusive date range are added.
    """
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"unreadable suspension interval evidence {path}") from exc
    validate_contract("suspension_interval_evidence.v1.json", document)

    official_hosts = ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn")
    expanded: dict[str, set[dt.date]] = {}
    recorded_intervals: list[dict[str, Any]] = []
    seen: set[tuple[str, dt.date, dt.date]] = set()
    session_values = sorted(set(sessions))
    for entry in document["intervals"]:
        instrument = entry["instrument"]
        start_text = entry["start_date"]
        end_text = entry["end_date"]
        try:
            start = dt.date.fromisoformat(start_text)
            end = dt.date.fromisoformat(end_text)
        except ValueError as exc:
            raise ContractError("suspension interval dates must be YYYY-MM-DD") from exc
        if start.isoformat() != start_text or end.isoformat() != end_text or end < start:
            raise ContractError("suspension interval dates must be ordered YYYY-MM-DD values")
        identity = (instrument, start, end)
        if identity in seen:
            raise ContractError(f"duplicate suspension interval for {instrument}")
        seen.add(identity)

        for reference in entry["evidence"]:
            host = (urlsplit(reference["url"]).hostname or "").lower()
            if not any(host == domain or host.endswith(f".{domain}") for domain in official_hosts):
                raise ContractError(
                    f"suspension evidence URL is not on a supported issuer/exchange domain: {host}"
                )

        covered = [session for session in session_values if start <= session <= end]
        expanded.setdefault(instrument, set()).update(covered)
        recorded_intervals.append({
            "instrument": instrument,
            "start_date": start_text,
            "end_date": end_text,
            "session_dates_applied": [session.isoformat() for session in covered],
            "evidence": entry["evidence"],
        })

    digest = hashlib.sha256(raw).hexdigest()
    notes["suspension_interval_evidence"] = {
        "path": str(path),
        "sha256": digest,
        "interval_count": len(recorded_intervals),
        "intervals": recorded_intervals,
    }
    notes["suspension_interval_evidence_sha256"] = digest
    return {instrument: sorted(values) for instrument, values in expanded.items()}


def merge_suspension_dates(
    base: dict[str, list[dt.date]], supplemental: dict[str, list[dt.date]]
) -> tuple[dict[str, list[dt.date]], int]:
    """Union supplemental interval sessions into the governed daily evidence."""
    merged = {instrument: set(days) for instrument, days in base.items()}
    added = 0
    for instrument, days in supplemental.items():
        prior = merged.setdefault(instrument, set())
        added += len(set(days) - prior)
        prior.update(days)
    return {instrument: sorted(days) for instrument, days in merged.items()}, added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validate-training-readiness")
    parser.add_argument("--bars-root", type=Path, required=True, help="canonical store root")
    parser.add_argument("--membership-root", type=Path, required=True, help="index membership store root")
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--min-sessions", type=int, default=60)
    parser.add_argument(
        "--warmup-sessions",
        type=int,
        default=60,
        help="walk-forward warmup before the first model-visible session (default 60)",
    )
    parser.add_argument("--label-horizon", type=int, default=5)
    parser.add_argument("--max-null-rate", type=float, default=0.0)
    parser.add_argument(
        "--suspension-interval-seconds",
        type=float,
        default=0.5,
        help="pause between suspend_d gap-session queries (default 0.5s stays under the 200/min 2000-point cap)",
    )
    parser.add_argument(
        "--skip-suspensions",
        action="store_true",
        help="do not query suspend_d; bar gaps are then reported without an explained/unexplained verdict",
    )
    parser.add_argument(
        "--suspension-snapshot-root",
        type=Path,
        default=None,
        help="read exact per-session suspend_d(S) snapshots instead of querying the live endpoint",
    )
    parser.add_argument(
        "--suspension-interval-evidence",
        type=Path,
        default=None,
        help=(
            "supplement daily suspend_d with a schema-validated interval file citing issuer or "
            "exchange notices; intervals are expanded over declared sessions and bound by SHA-256"
        ),
    )
    parser.add_argument(
        "--skip-delistings",
        action="store_true",
        help="do not query stock_basic(list_status=D); delisted members stay unexplained gaps",
    )
    parser.add_argument(
        "--delisting-snapshot-generation",
        help="read a checksum-verified stock_basic(list_status=D) snapshot with delist_date",
    )
    parser.add_argument(
        "--delisting-snapshot-root",
        type=Path,
        default=Path("data/trading-grade-history/source"),
        help="Tushare dependency store root for --delisting-snapshot-generation",
    )
    parser.add_argument("--schema", type=Path, default=Path("config/schemas/bars_daily.v2.yaml"))
    parser.add_argument(
        "--factor-partition-set",
        type=Path,
        default=None,
        help=(
            "optional factor_partition_set binding JSON (or a bare list of dates): asserts the "
            "feature side never reaches past the last labelable decision date"
        ),
    )
    parser.add_argument("--exchange", default="SSE")
    parser.add_argument(
        "--excluded-instruments",
        type=Path,
        default=None,
        help=(
            "JSON object mapping an instrument to the recorded reason it is dropped from "
            "the universe for this window (long halts the provider stops publishing, vendor "
            "holes). Reported as informational; an exclusion for a name that is not actually "
            "defective is itself a blocking finding."
        ),
    )
    parser.add_argument(
        "--independent-calendar",
        action="store_true",
        help="cross-check every session against an EastMoney index session list",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.suspension_interval_seconds < 0:
        raise SystemExit("--suspension-interval-seconds must not be negative")
    if args.warmup_sessions < 0:
        raise SystemExit("--warmup-sessions must not be negative")
    if args.skip_suspensions and args.suspension_snapshot_root is not None:
        raise SystemExit("--skip-suspensions cannot be combined with --suspension-snapshot-root")
    if args.skip_suspensions and args.suspension_interval_evidence is not None:
        raise SystemExit("--skip-suspensions cannot be combined with --suspension-interval-evidence")
    if args.skip_delistings and args.delisting_snapshot_generation:
        raise SystemExit("--skip-delistings cannot be combined with --delisting-snapshot-generation")

    schema = load_schema(args.schema)
    adapter = Tushare2kAdapter()
    notes: dict[str, Any] = {
        "calendar_provenance": f"tushare:trade_cal:{args.exchange}",
        # The verdict depends on these thresholds, so the report carries them
        # rather than relying on the caller's command line: the A500 pilot runs
        # with --min-sessions 17 (below the 60-session research default) because
        # its 23-session smoke window cannot satisfy the default.
        "min_sessions_per_instrument": args.min_sessions,
        "walk_forward_warmup_sessions": args.warmup_sessions,
        "label_horizon_sessions": args.label_horizon,
        "max_null_rate": args.max_null_rate,
        "suspension_interval_seconds": args.suspension_interval_seconds,
        "skip_suspensions": args.skip_suspensions,
        "skip_delistings": args.skip_delistings,
    }
    try:
        sessions = sorted(adapter.trade_dates(args.start.isoformat(), args.end.isoformat(), exchange=args.exchange))
    except ContractError as exc:
        print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
        return 2

    if args.independent_calendar:
        from uq.adapters.eastmoney import EastMoneySessionProvider

        try:
            provider = EastMoneySessionProvider()
            independent_sessions = sorted(provider.between(args.start, args.end))
            evidence = provider.evidence
        except (ContractError, RuntimeError) as exc:
            print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
            return 2
        notes["independent_calendar_provenance"] = provider.provenance
        notes["independent_calendar_evidence"] = evidence
        if set(sessions) != set(independent_sessions):
            missing = sorted(set(sessions) - set(independent_sessions))
            extra = sorted(set(independent_sessions) - set(sessions))
            print(
                json.dumps(
                    {
                        "status": "configuration_failure",
                        "error": "independent calendar disagrees with the primary session calendar",
                        "missing_from_independent": [value.isoformat() for value in missing],
                        "extra_in_independent": [value.isoformat() for value in extra],
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        notes["independent_calendar_verified"] = True

    bars = _bars_for_sessions(ManifestFirstReader(args.bars_root), schema, sessions, notes)
    membership = _members_by_session(IndexMembershipStore(args.membership_root), args.universe_id, sessions, notes)
    suspensions: dict[str, list[dt.date]] | None = None
    if args.suspension_snapshot_root is not None:
        try:
            suspensions = suspensions_from_snapshots(args.suspension_snapshot_root, sessions, notes)
        except (ContractError, OSError, ValueError) as exc:
            print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
            return 2
    elif not args.skip_suspensions:
        # An upstream failure here must look like what it is - a gate that
        # could not run - not like a blocked verdict with no report behind it.
        try:
            suspensions = _suspensions_for_gaps(
                adapter,
                sessions,
                membership,
                bars,
                notes,
                request_interval_seconds=args.suspension_interval_seconds,
            )
        except ContractError as exc:
            print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
            return 2
    if args.suspension_interval_evidence is not None:
        try:
            interval_suspensions = suspension_intervals_from_document(
                args.suspension_interval_evidence, sessions, notes
            )
        except (ContractError, OSError, ValueError) as exc:
            print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
            return 2
        if suspensions is None:
            # --skip-suspensions is rejected above; retain this check if another
            # entry path ever makes the suspension baseline optional.
            print(json.dumps({
                "status": "configuration_failure",
                "error": "suspension interval evidence requires suspension checking",
            }, ensure_ascii=False))
            return 2
        suspensions, added = merge_suspension_dates(suspensions, interval_suspensions)
        notes["suspension_interval_sessions_added"] = added
        notes["suspension_provenance"] = (
            f"{notes.get('suspension_provenance', 'unspecified')}"
            f"+official_interval:{notes['suspension_interval_evidence_sha256']}"
        )
    delisted: set[str] | None = None
    delist_dates: dict[str, dt.date] | None = None
    delisted_provenance = "unspecified"
    delist_date_provenance = "unspecified"
    if args.delisting_snapshot_generation:
        try:
            manifest, frame = TushareDependencyStore(args.delisting_snapshot_root).read_frame(
                args.delisting_snapshot_generation, dataset="stock_basic"
            )
            delisted, delist_dates = delistings_from_snapshot(manifest, frame)
        except (ContractError, OSError, ValueError) as exc:
            print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
            return 2
        delisted_provenance = f"tushare_dependency:stock_basic:{args.delisting_snapshot_generation}"
        delist_date_provenance = delisted_provenance
        notes["delisting_snapshot_generation"] = args.delisting_snapshot_generation
        notes["delisting_snapshot_fetched_at"] = manifest["source_fetched_at"]
        notes["delisting_snapshot_checksum_sha256"] = manifest["data_artifact"]["checksum_sha256"]
        notes["delisted_universe_size"] = len(delisted)
        notes["delisted_date_count"] = len(delist_dates)
    elif not args.skip_delistings:
        try:
            delisted = adapter.delisted_instruments()
        except ContractError as exc:
            print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
            return 2
        delisted_provenance = "tushare:stock_basic(list_status=D)"
        notes["delisted_universe_size"] = len(delisted)
    excluded: dict[str, str] | None = None
    if args.excluded_instruments is not None:
        try:
            document = json.loads(args.excluded_instruments.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"unreadable excluded-instruments file {args.excluded_instruments}: {exc}") from exc
        if not isinstance(document, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in document.items()
        ):
            raise SystemExit("excluded-instruments file must be a JSON object of instrument -> reason")
        excluded = document
        notes["excluded_instruments"] = excluded
        notes["excluded_provenance"] = f"file:{args.excluded_instruments}"
    factor_sessions: list[dt.date] | None = None
    if args.factor_partition_set is not None:
        document = _read_factor_partition_set(args.factor_partition_set)
        factor_sessions = factor_sessions_from_document(document)
        notes["factor_partition_set"] = str(args.factor_partition_set)
        notes["factor_sessions"] = len(factor_sessions)
        if isinstance(document, dict) and document.get("set_digest_sha256"):
            notes["factor_partition_set_digest"] = document["set_digest_sha256"]

    report = assess_readiness(
        window_start=args.start,
        window_end=args.end,
        sessions=sessions,
        members_by_session=membership,
        bars=bars,
        min_sessions_per_instrument=args.min_sessions,
        warmup_sessions=args.warmup_sessions,
        label_horizon_sessions=args.label_horizon,
        max_null_rate=args.max_null_rate,
        suspensions=suspensions,
        suspension_provenance=str(notes.get("suspension_provenance", "unspecified")),
        delisted=delisted,
        delisted_provenance=delisted_provenance,
        delist_dates=delist_dates,
        delist_date_provenance=delist_date_provenance,
        excluded_instruments=excluded,
        excluded_provenance=str(notes.get("excluded_provenance", "unspecified")),
        factor_sessions=factor_sessions,
    )
    payload = report.to_json()
    payload["universe_id"] = args.universe_id
    payload["notes"] = notes
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "sessions": payload["sessions"],
                "members": payload["members"],
                "instruments_with_bars": payload["instruments_with_bars"],
                "usable_label_end": payload["usable_label_end"],
                "finding_codes": sorted({item["code"] for item in payload["findings"]}),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
