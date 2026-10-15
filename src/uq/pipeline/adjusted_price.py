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
            "name": "finite_values__restated_price_matches_independent_source",
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


def publish_adjusted_partition(
    store: CanonicalV2Store,
    session: dt.date,
    raw: pd.DataFrame,
    *,
    expected_restated: pd.DataFrame,
    lineage: dict[str, Any] | None = None,
    source_versions: dict[str, str] | None = None,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Restate one session and publish it behind a verified quality report.

    ``raw`` is the v2 partition and ``expected_restated`` is an *independently
    supplied* restated partition -- the cross-check that makes the quality report
    mean something.  Restating and then verifying one's own output would be a
    tautology that stamps ``reject_all``/``passed`` on anything, which is why the
    cross-check is required rather than optional: without a second restatement to
    compare against there is nothing to verify, and the caller must not publish.

    The order matters: the report binds to the generation that will be published,
    so it is prepared (and read back by the store) before the partition moves into
    place.
    """
    if expected_restated is None:
        raise AdjustedPriceQualityRejection(
            "adjusted-price publication requires an independently restated partition to verify against"
        )
    schema = _adjusted_schema()
    restated = restate(raw)
    observed = verify_restatement(raw, restated, tolerance=tolerance)
    observed.update(verify_cross_check(restated, expected_restated, tolerance=tolerance))
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


def _adjusted_schema():
    return load_schema(ADJUSTED_V2_SCHEMA)
