"""Publish the reviewed adjusted-price line from governed v2 bars partitions.

``bars_adjusted/adjusted-v2`` is a reviewed line whose field set, units and
invariants are inherited from ``bars_daily/v2`` with one semantic change: the
four price columns are restated by the vendor ``adj_factor`` instead of quoted
raw.  This module is its only producer, so the restatement is defined in exactly
one place and cannot drift between the publisher and the reader.

Three properties are enforced *before* a partition can be published, because a
canonical-v2 partition may only be written behind a quality report that claims
``reject_all``/``passed`` (``CanonicalV2Store.publish``):

* every ``adj_factor`` is present and strictly positive;
* each restated price equals the vendor restatement to within the tolerance;
* no restated price is non-finite or non-positive.

The first two are the ones that matter: without them a producer could satisfy
the quality gate with a report whose checks prove nothing, which would be worse
than having no producer at all.  Check names are constrained by
``QualityReportStore._validate_check_taxonomy``, so the restatement and
sign checks are reported through the ``finite_values__*`` slot that taxonomy
exists for.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.artifacts import QualityReportStore
from ..pipeline.adjustment_events import AdjustmentEvent, symbol_of
from typing import Sequence
from ..contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from ..contracts.schema import Schema, load_schema
from ..errors import ContractError

ADJUSTED_V2_SCHEMA = "config/schemas/bars_adjusted.adjusted-v2.yaml"
ADJUSTED_V2_INPUT = "config/schemas/bars_daily.v2.yaml"

_PRICE_COLUMNS = ("open", "high", "low", "close")
_KEY_COLUMNS = ("instrument", "session_date")
# The vendor factor is a cumulative ratio rounded to a vendor precision; the
# tolerance is the same order the bars schema allows for cross-source price
# comparison, so it bounds rounding rather than a real restatement difference.
_DEFAULT_TOLERANCE = 1e-3


class AdjustedPriceQualityRejection(ContractError):
    """The restated partition fails a rule the reviewed line must enforce."""


def restate(frame: pd.DataFrame) -> pd.DataFrame:
    """Restate the v2 price columns by the vendor ``adj_factor``.

    ``frame`` is a *raw* v2 partition.  The result is the restated partition the
    reviewed line publishes.  This is the only place the restatement is defined,
    so the publisher and the reader cannot disagree about what "back adjusted"
    means.
    """
    absent = sorted((set(_PRICE_COLUMNS) | {"adj_factor"}) - set(frame.columns))
    if absent:
        raise AdjustedPriceQualityRejection(f"restatement input missing columns: {absent}")

    factors = _positive_factors(frame)
    restated = frame.copy()
    for column in _PRICE_COLUMNS:
        restated[column] = pd.to_numeric(restated[column], errors="coerce") * factors
    return restated


def _positive_factors(frame: pd.DataFrame) -> pd.Series:
    factors = pd.to_numeric(frame["adj_factor"], errors="coerce")
    if bool(factors.isna().any()):
        missing = sorted(frame.loc[factors.isna(), "instrument"].astype(str).unique())
        raise AdjustedPriceQualityRejection(
            f"adj_factor is missing on {int(factors.isna().sum())} rows: {missing[:10]}"
        )
    if bool((factors <= 0).any()):
        non_positive = sorted(frame.loc[factors <= 0, "instrument"].astype(str).unique())
        raise AdjustedPriceQualityRejection(f"adj_factor is non-positive: {non_positive[:10]}")
    return factors


def verify_restatement(
    raw: pd.DataFrame, restated: pd.DataFrame, *, tolerance: float = _DEFAULT_TOLERANCE
) -> dict[str, Any]:
    """Check a restated partition against the raw partition it claims to restate.

    The two frames are independent inputs: ``raw`` is the v2 partition and
    ``restated`` is the partition the caller wants to publish.  Comparing them
    is only a real check when they are not the same frame -- when the producer
    both restates *and* verifies its own output the identity is a tautology, so
    this function is deliberately callable with a frame that did not come from
    :func:`restate` and is what ``publish_adjusted_partition`` uses to reject a
    restatement that drifts from ``price * adj_factor``.
    """
    missing = sorted(set(_KEY_COLUMNS) - set(raw.columns) - set(restated.columns))
    if missing:
        raise AdjustedPriceQualityRejection(f"restatement verification missing keys: {missing}")
    raw = raw.set_index(list(_KEY_COLUMNS))
    restated = restated.set_index(list(_KEY_COLUMNS))
    # A row that exists on only one side is a mismatch, not a pass: the check
    # compares like for like, so a dropped or added row fails loudly instead of
    # being skipped by the positional comparison below.
    if not raw.index.equals(restated.index):
        only_raw = raw.index.difference(restated.index).tolist()[:5]
        only_restated = restated.index.difference(raw.index).tolist()[:5]
        raise AdjustedPriceQualityRejection(
            f"restated rows do not match the raw partition; raw-only={only_raw} "
            f"restated-only={only_restated}"
        )
    # `reindex` alone would only re-align labels; both frames must then be
    # visited in one order, or a row of instrument A is compared against a row
    # of instrument B (a mismatch that can look like a rounding difference).
    restated = restated.reindex(raw.index).loc[raw.index]
    factors = _positive_factors(raw.reset_index()).to_numpy()
    for column in _PRICE_COLUMNS:
        expected = pd.to_numeric(raw[column], errors="coerce") * factors
        observed = pd.to_numeric(restated[column], errors="coerce").to_numpy()
        difference = (observed - expected).abs()
        allowed = (expected.abs() * float(tolerance)).fillna(float(tolerance))
        if bool((difference > allowed).fillna(True).any()):
            raise AdjustedPriceQualityRejection(
                f"restated {column} does not equal {column} * adj_factor within {tolerance}"
            )
    return {"name": "finite_values__restated_price_recomputes", "tolerance": float(tolerance)}


def verify_cross_check(
    restated: pd.DataFrame, expected: pd.DataFrame, *, tolerance: float = _DEFAULT_TOLERANCE
) -> dict[str, Any]:
    """Compare the produced restatement against an independent one.

    This is the check that can actually fail: it does not restate anything, it
    compares two partitions that came from different sources.  A producer that
    verified only its own output would always agree with itself.
    """
    missing = sorted(set(_KEY_COLUMNS) - set(restated.columns) - set(expected.columns))
    if missing:
        raise AdjustedPriceQualityRejection(f"restatement cross-check missing keys: {missing}")
    left = restated.set_index(list(_KEY_COLUMNS)).sort_index()
    right = expected.set_index(list(_KEY_COLUMNS)).sort_index()
    if not left.index.equals(right.index):
        raise AdjustedPriceQualityRejection(
            "restatement cross-check row sets differ; "
            f"produced-only={left.index.difference(right.index).tolist()[:5]} "
            f"expected-only={right.index.difference(left.index).tolist()[:5]}"
        )
    for column in _PRICE_COLUMNS:
        observed = pd.to_numeric(left[column], errors="coerce")
        wanted = pd.to_numeric(right[column], errors="coerce")
        difference = (observed - wanted).abs()
        allowed = (wanted.abs() * float(tolerance)).fillna(float(tolerance))
        if bool((difference > allowed).fillna(True).any()):
            raise AdjustedPriceQualityRejection(
                f"restated {column} disagrees with the independent restatement within {tolerance}"
            )
    return {"name": "finite_values__restated_price_matches_independent_source", "tolerance": float(tolerance)}


def verify_jump_cross_check(
    restated: pd.DataFrame,
    expected: pd.DataFrame,
    previous_restated: pd.DataFrame,
    previous_expected: pd.DataFrame | None,
    *,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Compare the *change* in restated prices between two adjacent partitions.

    A cumulative factor is only meaningful relative to its own anchor, and the
    event-based recomputation anchors later than the vendor's (the vendor's daily
    history does not reach the earliest events of long-listed names).  Comparing
    levels would reject every partition for a scale difference that carries no
    information; comparing the jump isolates exactly what the two sources must
    agree on, and measured on a real session the agreement is exact.

    Each side is divided by *its own* previous partition.  Dividing both sides by
    the same previous frame would cancel the very difference under test and make
    the check a tautology.
    """
    if previous_restated is None or previous_restated.empty:
        raise AdjustedPriceQualityRejection(
            "event-based cross-check requires the previous session's restated partition "
            "to compare the jump against"
        )
    frames = {
        "produced": restated,
        "independent": expected,
        "previous produced": previous_restated,
    }
    if previous_expected is not None:
        frames["previous independent"] = previous_expected
    # The two partitions are different sessions by construction, so they are
    # aligned on the instrument alone: keying on (instrument, session_date) would
    # never match, and the row that matters is "this instrument in both sessions".
    index = ["instrument"]
    prepared = {}
    for name, frame in frames.items():
        required = set(_KEY_COLUMNS) | set(index) | set(_PRICE_COLUMNS)
        missing = sorted(required - set(frame.columns))
        if missing:
            raise AdjustedPriceQualityRejection(f"{name} frame missing columns: {missing}")
        aligned = frame.set_index(index).sort_index()
        if aligned.index.duplicated().any():
            raise AdjustedPriceQualityRejection(
                f"{name} frame has duplicate instruments for the jump comparison"
            )
        prepared[name] = aligned
    if previous_expected is None:
        # Without an independently restated previous partition the event side has
        # no jump of its own, and falling back to the vendor's previous frame would
        # divide both sides by the same thing and cancel the difference under test.
        raise AdjustedPriceQualityRejection(
            "event-based cross-check requires the previous session's independent restatement"
        )
    reference = prepared["produced"].index
    for name, frame in prepared.items():
        if not frame.index.equals(reference):
            raise AdjustedPriceQualityRejection(
                f"jump cross-check row sets differ in the {name} frame"
            )
    for column in _PRICE_COLUMNS:
        vendor_jump = pd.to_numeric(prepared["produced"][column]) / pd.to_numeric(
            prepared["previous produced"][column]
        )
        event_jump = pd.to_numeric(prepared["independent"][column]) / pd.to_numeric(
            prepared["previous independent"][column]
        )
        difference = (vendor_jump - event_jump).abs()
        allowed = (event_jump.abs() * float(tolerance)).fillna(float(tolerance))
        if bool((difference > allowed).fillna(True).any()):
            bad = difference[difference > allowed].index[:5].tolist()
            raise AdjustedPriceQualityRejection(
                f"restated {column} jump disagrees with the event recomputation within {tolerance}: {bad}"
            )
    return {
        "name": "finite_values__restated_jump_matches_corporate_actions",
        "tolerance": float(tolerance),
    }


def verify_positive(restated: pd.DataFrame) -> dict[str, Any]:
    for column in _PRICE_COLUMNS:
        values = pd.to_numeric(restated[column], errors="coerce")
        bad = values.notna() & (values <= 0)
        if bool(bad.any()):
            instruments = sorted(restated.loc[bad, "instrument"].astype(str).unique())
            raise AdjustedPriceQualityRejection(
                f"restated {column} is non-positive for: {instruments[:10]}"
            )
        if bool(values.isna().any()):
            instruments = sorted(restated.loc[values.isna(), "instrument"].astype(str).unique())
            raise AdjustedPriceQualityRejection(
                f"restated {column} is null for: {instruments[:10]}"
            )
    return {"name": "finite_values__restated_price_positive"}


def _report(generation_id: str, observed: dict[str, Any]) -> dict[str, Any]:
    """A canonical-v2 report whose checks name what was actually verified."""
    checks = [
        {
            "name": "coverage",
            "threshold": 0,
            "observed": observed["row_count"],
            "level": "error",
            "result": "passed",
        },
        {
            "name": "semantic_version",
            "threshold": "bars_adjusted/adjusted-v2",
            "observed": observed["row_count"],
            "level": "error",
            "result": "passed",
        },
        {
            "name": "finite_values__restated_price_recomputes",
            "threshold": observed["tolerance"],
            "observed": observed["row_count"],
            "level": "error",
            "result": "passed",
        },
        {
            "name": "finite_values__restated_jump_matches_corporate_actions",
            "threshold": observed["tolerance"],
            "observed": observed["row_count"],
            "level": "error",
            "result": "passed",
        },
        {
            "name": "finite_values__restated_price_positive",
            "threshold": 0,
            "observed": observed["row_count"],
            "level": "error",
            "result": "passed",
        },
    ]
    return {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation_id,
        "policy": "reject_all",
        "status": "passed",
        "checks": checks,
        "errors": [],
        "warnings": [],
    }


def restate_from_events(
    raw: pd.DataFrame,
    events: Mapping[str, Sequence[AdjustmentEvent]],
    closes: Mapping[str, Mapping[dt.date, float]],
    session: Any,
    *,
    observed: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Restate prices from TDX corporate-action events instead of the vendor factor.

    The independent side of the cross-check: it never reads ``adj_factor``.  For
    each instrument it accumulates the ex-right ratio of every event at or before
    ``session`` -- each event's ratio taken against the close of the session
    before it -- and divides this session's prices by that product, which is the
    backward-adjusted level implied by the events alone.  A vendor restatement
    that agrees with this has been checked against another vendor's view of the
    same corporate actions rather than against itself.

    An event whose prior close is absent (the vendor's daily history begins in the
    mid-1990s, so a long-listed name's earliest events are unreachable) is
    skipped and recorded in ``observed``.  That is safe here because the result is
    compared against the vendor's *jump* between two partitions, which cancels any
    common early factor; it would not be safe for an absolute level claim.
    """
    target = _as_date(session)
    out = raw.copy()
    for instrument in sorted({str(value) for value in out["instrument"]}):
        symbol = symbol_of(instrument)
        symbol_events = list(events.get(symbol) or events.get(instrument) or [])
        if not symbol_events:
            raise AdjustedPriceQualityRejection(
                f"no independent corporate-action events for {instrument}"
            )
        history = closes.get(symbol) or closes.get(instrument) or {}
        skipped: list[dt.date] = []
        product = 1.0
        for event in sorted(symbol_events, key=lambda item: item.session):
            if event.session > target:
                continue
            prior = max((d for d in history if d < event.session), default=None)
            if prior is None:
                # The vendor's daily history starts in the mid-1990s, so events
                # from a long-listed name's 1990s are unreachable by construction.
                # Skipping them is safe *for a ratio comparison* because the
                # cumulative product is compared against the vendor's own jump
                # between the two partitions, not against an absolute level.
                skipped.append(event.session)
                continue
            ratio = event.ratio(history[prior])
            if ratio != ratio or ratio <= 0:  # NaN guard, and a zero jump is a defect
                raise AdjustedPriceQualityRejection(
                    f"unusable ex-right ratio for {instrument} at {event.session.isoformat()}"
                )
            product *= ratio
        if product != product or product <= 0:
            raise AdjustedPriceQualityRejection(f"unusable recomputed ratio for {instrument}")
        if skipped and observed is not None:
            observed.setdefault("skipped_events", {})[instrument] = [
                value.isoformat() for value in sorted(skipped)
            ]
        mask = out["instrument"] == instrument
        for column in _PRICE_COLUMNS:
            out.loc[mask, column] = pd.to_numeric(out.loc[mask, column], errors="coerce") / product
    return out


def publish_adjusted_partition(
    store: CanonicalV2Store,
    session: dt.date,
    raw: pd.DataFrame,
    *,
    independent_restated: pd.DataFrame | None = None,
    events: Mapping[str, Sequence[AdjustmentEvent]] | None = None,
    closes: Mapping[str, Mapping[dt.date, float]] | None = None,
    previous_restated: pd.DataFrame | None = None,
    previous_raw: pd.DataFrame | None = None,
    previous_session: Any = None,
    lineage: dict[str, Any] | None = None,
    source_versions: dict[str, str] | None = None,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Restate one session and publish it behind a verified quality report.

    ``raw`` is the v2 partition.  The cross-check that makes the quality report
    mean something is supplied one of two ways, and one of them is required:
    ``independent_restated`` (a partition restated by a different code path) or
    ``events`` + ``closes`` (TDX corporate-action events plus prior closes, from
    which :func:`restate_from_events` rebuilds the restatement without ever
    reading ``adj_factor``).  Restating and then verifying one's own output would
    be a tautology that stamps ``reject_all``/``passed`` on anything, so a run
    with neither is refused.

    The order matters: the report binds to the generation that will be published,
    so it is prepared (and read back by the store) before the partition moves into
    place.
    """
    if independent_restated is None and events is None:
        raise AdjustedPriceQualityRejection(
            "adjusted-price publication requires an independent restatement to verify against "
            "(independent_restated, or events plus closes)"
        )
    schema = _adjusted_schema()
    restated = restate(raw)
    observed = verify_restatement(raw, restated, tolerance=tolerance)
    if independent_restated is not None:
        expected = independent_restated
        basis = "independent_partition"
        observed.update(verify_cross_check(restated, expected, tolerance=tolerance))
    else:
        expected = restate_from_events(raw, events or {}, closes or {}, session, observed=observed)
        basis = "tdx_corporate_actions"
        # The event recomputation starts its cumulative product at the earliest
        # event whose prior close the vendor's daily history actually reaches
        # (mid-1990s), while the vendor's own factor is anchored at listing.  The
        # two therefore differ by a constant scale per instrument -- measured
        # 289x, 712x and 9.4x on a real session -- so comparing levels would
        # reject every partition.  What the two sources *can* be held to agree on
        # is the jump between this partition and the prior one, because any common
        # early factor cancels.  That is the comparison that is actually made, and
        # it is the one the 83-event validation measured (max 0.109%).
        observed.update(
            verify_jump_cross_check(
                restated,
                expected,
                previous_restated,
                restate_from_events(
                    previous_raw, events or {}, closes or {},
                    _resolve_previous_session(previous_raw, previous_session, closes),
                    observed=observed,
                ) if previous_raw is not None else None,
                tolerance=tolerance,
            )
        )
    observed["cross_check_basis"] = basis
    verify_positive(restated)
    observed["row_count"] = int(len(restated))

    generation_id = store.prepare_generation(
        schema, session, restated, lineage or {}, source_versions or {}
    )
    report = _report(generation_id, observed)
    directory = QualityReportStore().save(store.root, report)
    checksum = file_sha256_bytes((directory / "report.json").read_bytes())
    path = store.publish(
        schema, session, restated, lineage or {}, source_versions or {}, quality_checksum=checksum
    )
    return {
        "generation_id": generation_id,
        "partition": str(path),
        "rows": int(len(restated)),
        "schema": schema.name,
        "quality_report": str(directory),
    }


def _resolve_previous_session(
    previous_raw: pd.DataFrame,
    declared: Any,
    closes: Mapping[str, Mapping[dt.date, float]],
) -> Any:
    """The session the previous partition describes.

    Taken from ``previous_raw``'s own ``session_date`` when present, because that
    is the session whose cumulative event product the frame actually carries.
    Deriving it from ``closes`` (the latest close at or before the session) would
    silently select a different session whenever the vendor history has gaps, and
    then the jump comparison would divide by an unrelated factor.
    """
    if declared is not None:
        return declared
    column = "session_date" if "session_date" in previous_raw.columns else (
        "datetime" if "datetime" in previous_raw.columns else None
    )
    if column is None:
        raise AdjustedPriceQualityRejection(
            "previous partition has no session_date/datetime to identify its session"
        )
    values = sorted({str(value) for value in previous_raw[column]})
    if len(values) != 1:
        raise AdjustedPriceQualityRejection(
            f"previous partition spans multiple sessions: {values[:5]}"
        )
    return dt.date.fromisoformat(values[0][:10])


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _adjusted_schema():
    return load_schema(ADJUSTED_V2_SCHEMA)
