"""Regression locks for the external review of 2026-10-13 (code-review-unified-quant.md).

The report was written against ``44a0286``; every item here was re-verified against
this tree before being fixed, and each test fails on the pre-fix code:

* **B7** -- ``uq.contracts.schema`` carried its own ``ContractError``, so a schema
  rejection escaped ``except ContractError`` in the ingest pipeline and lost its
  error classification;
* **B8** -- the template digest-mismatch message read ``manifest_digest_sha256``,
  a field the template families do not carry, so the governance rejection died as
  a bare ``KeyError``;
* **B12** -- purge/embargo was compared against the splits iterated so far, so a
  legitimate ``[validation, train]`` declaration was rejected as a leak;
* **B4** -- the backtest executed buys and sells in one alphabetical pass despite
  the ``sells first`` comment, so a fundable rebalance recorded an alphabetically
  early buy as ``skipped_insufficient_cash``;
* **B10a/B10b** -- the turnover cap had no stated answer for "what came before":
  the first rebalance of every run died on ``max_turnover > 0`` while
  ``max_turnover == 0`` skipped the constraint entirely;
* **B6** -- the prediction stage published a state inside its per-decision-date
  loop, and state paths are keyed by stage number, so every multi-date run died
  on ``immutable research manifest already exists``;
* **B9** -- the symlink guards walked every lexical ancestor, so a store root
  under macOS's ``/private`` shims (or any symlinked parent) was refused, while
  ``pytest``'s resolved ``tmp_path`` hid it from every test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import tempfile

from uq.contracts import schema as schema_module
from uq.contracts.schema import load_schema
from uq.backtest.engine import BacktestEngine
from uq.contracts.model_layer import ModelContractLoader
from uq.contracts.paths import reject_symlinks_within
from uq.errors import ContractError
from uq.portfolio.builder import PortfolioBuilder
from uq.models.dataset import SplitValidator
from tests.test_backtest_layer import DATES, _make_config, _make_price_panel, _make_weights
from tests.test_portfolio_layer import _make_definition

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_FIXTURE = (
    ROOT / "evidence/research-chain/phase-0/fixtures/model_definition_template-valid.json"
)
V2_SCHEMA = ROOT / "config/schemas/bars_daily.v2.yaml"


def dates(count: int = 30) -> list[str]:
    return [day.strftime("%Y-%m-%d") for day in pd.bdate_range("2026-01-01", periods=count)]


def test_schema_and_governed_code_share_one_contract_error_type():
    assert schema_module.ContractError is ContractError


def test_schema_rejection_is_catchable_as_the_governed_contract_error():
    schema = load_schema(V2_SCHEMA)
    frame = pd.DataFrame([{"instrument": "600000.XSHG"}])
    with pytest.raises(ContractError):
        schema.validate(frame)


def test_template_digest_mismatch_raises_contract_error_not_key_error():
    payload = json.loads(TEMPLATE_FIXTURE.read_text(encoding="utf-8"))
    assert "manifest_digest_sha256" not in payload, "the fixture stopped matching the template family"
    payload["template_manifest_digest_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="manifest digest mismatch"):
        ModelContractLoader.validate("model_definition_template", payload)


def test_split_rules_do_not_depend_on_declaration_order():
    sessions = dates()
    train = {"name": "train", "start_date": sessions[0], "end_date": sessions[14]}
    validation = {"name": "validation", "start_date": sessions[25], "end_date": sessions[29]}
    for declared in ([train, validation], [validation, train]):
        SplitValidator.validate_splits(
            declared, horizon=5, embargo_days=2, trading_dates=sessions,
        )


def test_declared_out_of_order_purge_violation_still_fails():
    sessions = dates()
    train = {"name": "train", "start_date": sessions[0], "end_date": sessions[14]}
    validation = {"name": "validation", "start_date": sessions[17], "end_date": sessions[29]}
    for declared in ([train, validation], [validation, train]):
        with pytest.raises(ContractError, match="purge/embargo violation between train and validation"):
            SplitValidator.validate_splits(
                declared, horizon=5, embargo_days=2, trading_dates=sessions,
            )


def test_rebalance_sells_settle_before_buys_so_fundable_buys_are_not_skipped(tmp_path):
    """A rebalance that only fits after the exit proceeds must actually execute (B4).

    ``ZELT`` sorts after ``AAA``: with one alphabetical pass the buy of ``AAA`` is
    attempted while cash is still tied up in ``ZELT`` and gets recorded
    ``skipped_insufficient_cash``, even though selling ``ZELT`` funds it.
    """
    instruments = ["AAA", "ZELT"]
    engine = BacktestEngine(tmp_path)
    manifest, artifacts = engine.run(
        config=_make_config(initial_capital=30000.0),
        portfolio_definition={"generation_id": "b" * 64},
        weight_partitions={
            "2026-01-05": _make_weights("2026-01-05", ["ZELT"]),
            "2026-01-06": _make_weights("2026-01-06", ["AAA"]),
        },
        price_panel=_make_price_panel(DATES, instruments),
    )
    fills = artifacts["fills"]
    day_one = fills[fills["date"] == "2026-01-06"]
    assert list(day_one[(day_one["side"] == "buy") & (day_one["status"] == "filled")]["instrument"]) == ["ZELT"], \
        "the scenario needs an opening position, otherwise the assertion below is vacuous"

    rebalance = fills[fills["date"] == "2026-01-07"]
    sold = rebalance[(rebalance["side"] == "sell") & (rebalance["status"] == "filled")]
    bought = rebalance[(rebalance["side"] == "buy") & (rebalance["status"] == "filled")]
    assert list(sold["instrument"]) == ["ZELT"]
    assert list(bought["instrument"]) == ["AAA"]
    assert int(bought.iloc[0]["filled_shares"]) > 0
    assert (rebalance["status"] != "skipped_insufficient_cash").all()
    assert manifest["schema_version"]


def _turnover_definition(max_turnover: float) -> dict:
    return _make_definition(constraints={
        "max_single_weight": 1.0, "max_industry_weight": None,
        "max_turnover": max_turnover, "cash_reserve": 0.0,
    })


def _scores() -> pd.Series:
    return pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0, "E": 1.0})


def test_turnover_cap_rejects_an_unstated_previous_state(tmp_path):
    builder = PortfolioBuilder(tmp_path)
    with pytest.raises(ContractError, match="previous_target_weights is required"):
        builder.build(
            definition=_turnover_definition(0.3), prediction_generation_id="b" * 64,
            decision_date="2026-01-05", scores=_scores(),
            universe_instruments=["A", "B", "C", "D", "E"],
        )


def test_run_initialization_may_build_without_previous_weights(tmp_path):
    builder = PortfolioBuilder(tmp_path)
    manifest, frame = builder.build(
        definition=_turnover_definition(0.3), prediction_generation_id="b" * 64,
        decision_date="2026-01-05", scores=_scores(),
        universe_instruments=["A", "B", "C", "D", "E"],
        initial_state=True,
    )
    assert manifest["previous_target_weights_generation_id"] is None
    assert sorted(frame["instrument"]) == ["A", "B", "C"]
    assert frame["weight"].sum() == pytest.approx(1.0, abs=1e-9)


def test_zero_turnover_cap_freezes_the_existing_position(tmp_path):
    builder = PortfolioBuilder(tmp_path)
    _, frame = builder.build(
        definition=_turnover_definition(0.0), prediction_generation_id="b" * 64,
        decision_date="2026-01-06", scores=_scores(),
        universe_instruments=["A", "B", "C", "D", "E"],
        previous_target_weights={"A": 1.0},
    )
    assert list(frame["instrument"]) == ["A"]
    assert float(frame.iloc[0]["weight"]) == pytest.approx(1.0, abs=1e-9)

def test_zero_turnover_cap_cannot_slip_through_an_unset_previous_state(tmp_path):
    """The B10b shape: a freeze cap with no previous state must refuse, not build.

    Pre-fix the two guards disagreed -- ``max_turnover > 0`` demanded the previous
    weights while ``max_turnover == 0`` entered neither branch and built a full
    position, so tightening 0.0 to 0.0001 changed behavior in the wrong direction.
    """
    builder = PortfolioBuilder(tmp_path)
    with pytest.raises(ContractError, match="previous_target_weights is required"):
        builder.build(
            definition=_turnover_definition(0.0), prediction_generation_id="b" * 64,
            decision_date="2026-01-05", scores=_scores(),
            universe_instruments=["A", "B", "C", "D", "E"],
            previous_target_weights=None, initial_state=False,
        )


def test_prediction_stage_publishes_every_decision_date_in_one_state(tmp_path):
    """Two decision dates must produce one stage state with two bindings (B6)."""
    from tests.test_research_chain_phase3 import prediction_decision
    from tests.test_research_chain_phase4 import (
        DATES, INSTRUMENTS, RUNNER, _extended_plan, _publish_model_artifact,
    )
    from uq.models.predictions import PredictionBuilder
    from uq.research_chain import FileResearchRunStore
    from uq.research_chain.adapters import ModelStageResult, PredictionStageAdapter
    from uq.research_chain.contracts import PublishedState

    import dataclasses

    from uq.research_chain.resolver import ResolvedStageBinding

    _, plan, _universe = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    # What the plan looks like once the dataset stage has published its output.
    plan = dataclasses.replace(plan, stage_bindings=(
        *plan.stage_bindings,
        ResolvedStageBinding(
            stage="dataset_preparation", output_family="model_dataset",
            generation_id="0" * 64, manifest_digest_sha256="1" * 64,
            data_checksum_sha256="2" * 64,
        ),
    ))
    dataset_binding = next(
        binding for binding in plan.stage_bindings if binding.output_family == "model_dataset"
    )
    run_generation, artifact_generation, artifact_checksum = _publish_model_artifact(tmp_path)
    model_result = ModelStageResult(
        run_manifest={"run_content_generation_id": run_generation},
        definition={},
        artifact_manifest={
            "generation_id": artifact_generation,
            "artifact_checksum_sha256": artifact_checksum,
        },
        artifact_bytes=b"",
        published_state=PublishedState(tmp_path / "unused", "0" * 64),
    )
    scores = pd.DataFrame({"instrument": INSTRUMENTS, "score": [0.9, 0.5, 0.1]})
    adapter = PredictionStageAdapter(PredictionBuilder(tmp_path), FileResearchRunStore(tmp_path))

    results = adapter.run_for_dates(
        plan,
        dataset_generation_id=dataset_binding.generation_id,
        model_stage_result=model_result,
        scores_by_decision_date={date: scores for date in DATES[:2]},
        quality_decision=prediction_decision(),
        eligibility_policy="reviewed-v1",
        eligibility_status="passed",
        runner_identity=RUNNER,
        created_at="2026-01-30T07:00:00+00:00",
    )

    assert len(results) == 2
    assert len({result.manifest["generation_id"] for result in results}) == 2
    state = json.loads(results[0].published_state.manifest_path.read_text(encoding="utf-8"))
    bindings = [
        binding
        for binding in state["stage_records"][-1]["output_bindings"]
        if binding["output_family"] == "prediction_set"
    ]
    assert {Path(binding["physical_path"]).name for binding in bindings} == {
        f"date={date}" for date in DATES[:2]
    }


def test_store_root_under_a_symlinked_ancestor_is_accepted(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "link"
    linked_root.symlink_to(real_root, target_is_directory=True)
    partition = linked_root / "runs" / "2026-01-05"
    partition.mkdir(parents=True)
    assert reject_symlinks_within(linked_root, partition).is_dir()


def test_symlink_inside_the_store_root_still_fails(tmp_path):
    root = tmp_path / "store"
    (root / "runs").mkdir(parents=True)
    (root / "runs" / "real").mkdir()
    (root / "runs" / "link").symlink_to(root / "runs" / "real", target_is_directory=True)
    with pytest.raises(ContractError, match="symbolic links are forbidden"):
        reject_symlinks_within(root, root / "runs" / "link")


def test_path_outside_the_store_root_is_refused(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    with pytest.raises(ContractError, match="outside the governed root"):
        reject_symlinks_within(root, tmp_path / "elsewhere")


def test_accepted_contract_loader_scopes_links_to_the_configured_root(tmp_path):
    accepted = tmp_path / "accepted"
    (accepted / "sub").mkdir(parents=True)
    view = tmp_path / "view"
    view.symlink_to(accepted, target_is_directory=True)
    contract = view / "sub" / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    assert ModelContractLoader._safe_resolve(contract, root=view) == contract.resolve()

    smuggled = view / "contract-copy.json"
    smuggled.symlink_to(contract)
    with pytest.raises(ContractError, match="symbolic links are forbidden"):
        ModelContractLoader._safe_resolve(smuggled, root=view)
