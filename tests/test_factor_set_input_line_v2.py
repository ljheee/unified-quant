"""Reviewed factor sets over the production ``bars_daily/v2`` input line (cr.md P3, option A).

The A500 / full-A pilot publishes ``canonical/bars_daily/v2`` partitions, while the
only reviewed raw-price definition accepted ``research-v1``.  Option A keeps the v1
definition and its published evidence frozen and adds ``basic/2.0.0`` as a new
reviewed definition whose ``input_bindings`` name ``bars_daily/v2``.

Equivalence that makes this a *new version* rather than a rename:
``config/schemas/bars_daily.v2.yaml`` declares the same units as research-v1
(``volume`` share, ``amount`` CNY, prices raw CNY) and records the key rename
``datetime -> session_date``; ``src/uq/adapters/tushare.py`` converts Tushare's
lots/千元 once for both lines (``* 100.0`` / ``* 1000.0``), so factor values are
unit-identical.  Factor frames themselves stay ``datetime``-keyed because
``_validate_factor_frame`` requires ``instrument``/``datetime`` regardless of the
input line.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tests.test_factor_store import ROOT, frame, kwargs
from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.gate_contracts import validate_contract_path
from uq.errors import ContractError
from uq.factors.store import FactorStore, _validate_factor_frame, factor_generation, read_factor_partition

BASIC_V1 = ("basic", "1.0.0", "bars_daily", "research-v1")
BASIC_V2 = ("basic", "2.0.0", "bars_daily", "v2")


def _publish(tmp_path: Path, *, factor_set: str, factor_version: str,
             input_dataset: str, input_schema_version: str, **overrides):
    """Publish one governed factor partition with its external quality report."""
    arguments = {
        **kwargs(),
        "factor_set": factor_set,
        "factor_version": factor_version,
        "input_dataset": input_dataset,
        "input_schema_version": input_schema_version,
        **overrides,
    }
    definition = FactorRegistry(ROOT).get(factor_set, factor_version)
    generation = factor_generation(**arguments)
    document = {
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
        "policy": definition.document["quality_policy"],
        "status": _validate_factor_frame(arguments["frame"], definition)["status"],
        "checks": _validate_factor_frame(arguments["frame"], definition)["checks"],
        "errors": [],
        "warnings": [],
    }
    QualityReportStore().save(tmp_path, document)
    arguments["quality_report_checksum"] = file_sha256_bytes(
        (tmp_path / "reports" / "factor_v1" / generation / "report.json").read_bytes()
    )
    return FactorStore(tmp_path, FactorRegistry(ROOT)).publish(**arguments)


def test_both_reviewed_versions_keep_their_own_input_line():
    registry = FactorRegistry(ROOT)
    for factor_set, factor_version, dataset, schema_version in (BASIC_V1, BASIC_V2):
        definition = registry.get(factor_set, factor_version)
        assert definition.document["status"] == "reviewed"
        bindings = definition.document.get("input_bindings")
        if bindings is None:
            # The v1 definition predates input_bindings; its reviewed pair is the
            # historical bars_daily/research-v1 line, asserted through behaviour below.
            assert (dataset, schema_version) == ("bars_daily", "research-v1")
            continue
        assert bindings["allowed_datasets"] == [dataset]
        assert bindings["required_schema_versions"][dataset] == [schema_version]


def test_v2_definition_does_not_widen_the_v1_definition():
    v1 = json.loads((ROOT / "config/factor-sets/basic-v1.json").read_text())
    v2 = json.loads((ROOT / "config/factor-sets/basic-v2.json").read_text())
    # A new input line is a new *version*, not an edit: implementations, thresholds
    # and output contract stay byte-identical, so v1 partitions keep their meaning.
    assert v2["factors"] == v1["factors"]
    assert v2["required_columns"] == v1["required_columns"]
    assert v2["quality_thresholds"] == v1["quality_thresholds"]
    assert v2["quality_policy"] == v1["quality_policy"]
    assert set(v2) - set(v1) <= {"input_bindings", "set_definition_version", "factor_version"}
    assert v1["set_definition_version"] == 1


def test_publish_over_v2_input_line(tmp_path):
    partition = _publish(tmp_path, factor_set="basic", factor_version="2.0.0",
                         input_dataset="bars_daily", input_schema_version="v2")
    assert partition == (
        tmp_path / "factors"
        / "dataset=bars_daily/schema_version=v2/factor_set=basic/factor_version=2.0.0/date=2026-07-21"
    )
    manifest = json.loads((partition / "manifest.json").read_text())
    assert manifest["input_schema_version"] == "v2"
    assert manifest["manifest_version"] == 1  # native set, not a qlib engine manifest
    assert len(read_factor_partition(partition)) == 1


def test_reviewed_line_is_enforced_in_both_directions(tmp_path):
    # basic/2.0.0 refuses the line that basic/1.0.0 requires ...
    with pytest.raises(ContractError, match=r"factor input schema is not reviewed: research-v1"):
        _publish(tmp_path / "a", factor_set="basic", factor_version="2.0.0",
                 input_dataset="bars_daily", input_schema_version="research-v1")
    # ... and basic/1.0.0 still refuses v2, i.e. adding 2.0.0 did not widen v1.
    with pytest.raises(ContractError, match="reviewed binding is bars_daily/research-v1"):
        _publish(tmp_path / "b", factor_set="basic", factor_version="1.0.0",
                 input_dataset="bars_daily", input_schema_version="v2")


def test_adjustment_lineage_cannot_be_bound_to_a_raw_price_input(tmp_path):
    lineage = {"adjustment_snapshot_id": "c" * 64, "effective_date_table_checksum": "d" * 64}
    with pytest.raises(ContractError, match="raw-price factors cannot bind adjustment lineage"):
        _publish(tmp_path / "v1", factor_set="basic", factor_version="1.0.0",
                 input_dataset="bars_daily", input_schema_version="research-v1", **lineage)
    with pytest.raises(ContractError, match="raw-price factors cannot bind adjustment lineage"):
        _publish(tmp_path / "v2", factor_set="basic", factor_version="2.0.0",
                 input_dataset="bars_daily", input_schema_version="v2", **lineage)


def test_unreviewed_dataset_is_named_with_the_reviewed_set(tmp_path):
    with pytest.raises(ContractError) as error:
        _publish(tmp_path, factor_set="basic", factor_version="2.0.0",
                 input_dataset="bars_adjusted", input_schema_version="v2")
    message = str(error.value)
    assert "factor input dataset is not reviewed: bars_adjusted" in message
    assert "reviewed datasets for basic/2.0.0: ['bars_daily']" in message


def test_definition_v3_keeps_qlib_members_conditional():
    schema = ROOT / "config/schemas/factor-sets/factor_set.v3.json"
    # A native set needs no qlib members and no qlib engine version.
    validate_contract_path(schema, json.loads((ROOT / "config/factor-sets/basic-v2.json").read_text()))

    qlib_like = json.loads((ROOT / "config/factor-sets/alpha158-v1.json").read_text())
    qlib_like["set_definition_version"] = 3
    validate_contract_path(schema, qlib_like)

    for removed in ("qlib_name", "expression"):
        modified = json.loads(json.dumps(qlib_like))
        del modified["factors"][0][removed]
        with pytest.raises(ContractError, match="is a required property"):
            validate_contract_path(schema, modified)

    missing_engine = json.loads(json.dumps(qlib_like))
    del missing_engine["reviewed_engine_versions"]
    with pytest.raises(ContractError):
        validate_contract_path(schema, missing_engine)

    # input_bindings stays mandatory in v3: without it the store would have to fall
    # back to a hardcoded pair, which is exactly what this version removes.
    missing_bindings = json.loads(json.dumps(json.loads(
        (ROOT / "config/factor-sets/basic-v2.json").read_text())))
    del missing_bindings["input_bindings"]
    with pytest.raises(ContractError):
        validate_contract_path(schema, missing_bindings)


def test_reviewed_alpha158_v2_binds_the_v2_line_and_stays_schema_conformant():
    # cr.md: alpha158/1.0.0 only accepted research-v1, so the factor line could not
    # run over the v2 partitions the pilots publish. The refreshed reviewed
    # definition keeps every expression/fingerprint/minimum_history identical and
    # changes only `input_bindings`, which is what makes it a new version rather
    # than a rename.
    v1 = json.loads((ROOT / "config" / "factor-sets" / "alpha158-v1.json").read_text())
    v2 = json.loads((ROOT / "config" / "factor-sets" / "alpha158-v2.json").read_text())

    assert (v1["factor_set"], v1["factor_version"]) == ("alpha158", "1.0.0")
    assert (v2["factor_set"], v2["factor_version"]) == ("alpha158", "2.0.0")
    assert v2["status"] == "reviewed"
    assert v1["input_bindings"]["required_schema_versions"] == {"bars_daily": ["research-v1"]}
    assert v2["input_bindings"]["required_schema_versions"] == {"bars_daily": ["v2"]}
    # the v1 definition is not widened by the new version, and the factor frames
    # stay datetime-keyed (the adapter requires it)
    assert v1["required_columns"][1] == "datetime"
    assert v2["required_columns"][1] == "datetime"
    # everything that defines the factors is inherited verbatim
    assert v2["factors"] == v1["factors"]
    assert v2["excluded_factors"] == v1["excluded_factors"]
    assert v2["reviewed_engine_versions"] == v1["reviewed_engine_versions"]
    assert v2["qlib_expression_set"] == v1["qlib_expression_set"]
    assert FactorRegistry(ROOT).get("alpha158", "2.0.0").factors


def test_alpha158_v2_is_refused_for_the_research_v1_input_line(tmp_path):
    # The input-line guard lives in FactorStore.publish. The fixture frame is a
    # basic-shaped frame, so the output-column check fires first for any
    # combination; what this locks is that a research-v1 input for the v2
    # definition never publishes (the refusal arrives before any partition or
    # report is written), not the wording of a specific error.
    store = FactorStore(tmp_path, FactorRegistry(ROOT))
    with pytest.raises(ContractError):
        store.publish(
            factor_set="alpha158", factor_version="2.0.0",
            frame=frame(), partition_date=date(2026, 8, 21),
            input_dataset="bars_daily", input_schema_version="research-v1",
            upstream_generation_id="a" * 64, upstream_data_checksum="b" * 64,
            quality_report_checksum="c" * 64,
            upstream_created_at="2026-08-21T00:00:00+00:00",
        )
    assert not list(tmp_path.rglob("manifest.json"))
    assert not list(tmp_path.rglob("report.json"))
