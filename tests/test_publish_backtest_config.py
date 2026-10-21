"""The `backtest_config.v1` artifact a research request binds.

`BacktestConfigStore.publish` derives the config identity from content and binds an
externally signed review, but nothing assembled the content until
`scripts/publish_backtest_config.py`. These tests drive the real script against
governed-shaped fixtures and the real stores, then assert the published config is
what the backtest stage and the request resolver actually read back.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.research_chain.owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    BacktestMarketDatasetStore,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_backtest_config.py"
MARKET_SCRIPT = ROOT / "scripts" / "publish_backtest_market_data.py"
START = dt.date(2026, 1, 5)
END = dt.date(2026, 1, 7)
SESSIONS = [dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]
UNIVERSE = "csi-all"
UNIVERSE_GENERATION = "1" * 64


def _adjusted_price(root: Path, sessions: list[dt.date], *, key: str = "session_date") -> str:
    """Publish one window-spanning adjusted-price partition and return its generation.

    Goes through `CanonicalV2Store` exactly as the research-chain fixtures do,
    because the canonical-v2 manifest's generation id and digest are derived by the
    store -- hand-writing them produces a manifest the governed read refuses.
    """
    from uq.contracts.artifacts import QualityReportStore
    from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
    from uq.contracts.schema import load_schema

    schema = load_schema("config/schemas/bars_adjusted.adjusted-v2.yaml")
    rows = []
    for session in sessions:
        for instrument in ("600000.XSHG", "600519.XSHG"):
            rows.append({
                "instrument": instrument, key: pd.Timestamp(session), "open": 10.0, "high": 11.0,
                "low": 9.0, "close": 10.5, "volume": 1000.0, "amount": 10000.0,
                "status": "trading", "limit_up": 11.0, "limit_down": 9.0,
                "adj_factor": 1.0,
            })
    frame = pd.DataFrame(rows)
    store = CanonicalV2Store(root)
    generation = store.prepare_generation(schema, sessions[0], frame, {}, {})
    report = {
        "report_version": 1, "binding_type": "canonical_v2",
        "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
        "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                    "level": "error", "result": "passed"}],
        "errors": [], "warnings": [],
    }
    report_directory = QualityReportStore().save(root, report)
    checksum = file_sha256_bytes((report_directory / "report.json").read_bytes())
    store.publish(schema, sessions[0], frame, {}, {}, quality_checksum=checksum)
    return generation


def _market_inputs(root: Path) -> dict:
    from tests.test_publish_backtest_market_data import _canonical_bars, _dividend_store

    _canonical_bars(root, SESSIONS)
    _dividend_store(root / "dividend-store", dt.date(2026, 1, 2))
    result = subprocess.run(
        [sys.executable, str(MARKET_SCRIPT), "--data-root", str(root),
         "--dividend-root", str(root / "dividend-store"),
         "--start", START.isoformat(), "--end", END.isoformat()],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _universe_snapshot(root: Path) -> str:
    """Publish a universe snapshot covering the window, through its own store.

    `UniverseSnapshotStore.save` derives the members checksum and the generation
    id, so hand-writing a manifest would fork that contract.
    """
    from uq.contracts.artifacts import UniverseSnapshotStore

    store = UniverseSnapshotStore()
    members = pd.DataFrame({"instrument": ["600000.XSHG", "600519.XSHG"]})
    store.save(
        root,
        {
            "universe_version": 1,
            "universe_id": UNIVERSE,
            "source": "tushare:index_weight:000985.CSI",
            "membership_evidence": (
                "source=tushare:index_weight:000985.CSI pit_precision=monthly_approximation"
            ),
            "snapshot_time": "2026-01-01T00:00:00+08:00",
            "valid_from": START.isoformat(),
            "valid_to": END.isoformat(),
            "visibility_time": START.isoformat() + "T00:00:00+08:00",
            # save() recomputes this from the bytes it writes; the schema only
            # requires the field present while the candidate is validated.
            "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
        },
        members,
    )
    published = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (root / "universes" / UNIVERSE).glob("*/manifest.json")
    ][0]
    # exactly one snapshot, and it is the one the window binds
    assert published["universe_id"] == UNIVERSE
    assert published["valid_from"] == START.isoformat()
    assert published["valid_to"] == END.isoformat()
    return published["generation_id"]


REVIEWER = ROOT / "tests" / "fixtures" / "backtest_config_reviewer.py"


def _reviewer_script(path: Path) -> Path:
    """Write a stand-in external reviewer that signs for the subject it is given.

    It models the production shape: a separate process, invoked with the derived
    subject, that returns a signed decision for exactly that subject. It is a
    test double for the maintainer's reviewer, not a shortcut around the review --
    the publisher still refuses to run without it.
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, sys\n"
        "sys.path.insert(0, {root!r})\n"
        "from tests.review_key import REVIEWER_PRIVATE_KEY\n"
        "from uq.contracts.model_layer import (\n"
        "    _MODEL_QUALITY_REVIEW_REGISTRY, create_reviewed_quality_decision)\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--binding-type', required=True)\n"
        "parser.add_argument('--subject-generation-id', required=True)\n"
        "parser.add_argument('--subject-manifest-digest-sha256', default='')\n"
        "parser.add_argument('--output-family', default='')\n"
        "parser.add_argument('--provider-config-ref', default='')\n"
        "args = parser.parse_args()\n"
        "registry = _MODEL_QUALITY_REVIEW_REGISTRY.bindings[args.binding_type]\n"
        "print(json.dumps(create_reviewed_quality_decision(\n"
        "    binding_type=args.binding_type, policy=registry['policy'], status='passed',\n"
        "    checks=[{{'name': n, 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}}\n"
        "            for n in registry['allowed_checks']],\n"
        "    errors=[], warnings=[], producer_code_fingerprint='0' * 64,\n"
        "    private_key_pem=REVIEWER_PRIVATE_KEY,\n"
        "    subject_generation_id=args.subject_generation_id,\n"
        "    subject_content_sha256=args.subject_generation_id)))\n"
        .format(root=str(ROOT)),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _decisions_document(path: Path, reviewer: Path) -> None:
    path.write_text(json.dumps({
        "provider_id": "external-model-quality-reviewer-v1",
        "provider_command": [sys.executable, str(reviewer)],
    }), encoding="utf-8")


def _run(root: Path, market: dict, price: str, universe: str, decisions: Path, **overrides):
    arguments = [
        sys.executable, str(SCRIPT),
        "--data-root", str(root), "--universe-root", str(root),
        "--universe-id", UNIVERSE, "--universe-snapshot", universe,
        "--adjusted-price-generation", price,
        "--market-inputs", str(root / "market-inputs.json"),
        "--start", START.isoformat(), "--end", END.isoformat(),
        "--backtest-name", "research-baseline",
        "--quality-decisions-json", str(decisions),
    ]
    for key, value in overrides.items():
        if value is not None:
            arguments += [f"--{key.replace('_', '-')}", str(value)]
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, arguments, result.stdout, result.stderr)
    return json.loads(result.stdout)


def test_publishes_a_config_the_store_and_backtest_both_read_back(tmp_path: Path) -> None:
    price = _adjusted_price(tmp_path, SESSIONS)
    market = _market_inputs(tmp_path)
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    _decisions_document(decisions, _reviewer_script(tmp_path / "reviewer.py"))

    report = _run(tmp_path, market, price, universe, decisions)
    store = BacktestConfigStore(tmp_path)
    manifest = store.read_manifest(report["generation_id"])
    assert manifest["generation_id"] == report["generation_id"]
    assert manifest["manifest_digest_sha256"] == report["manifest_digest_sha256"]

    # the four bindings are exactly what the inputs said
    assert manifest["universe_binding"] == {"snapshot_generation_id": universe}
    assert manifest["price_source_binding"]["dataset_generation_id"] == price
    assert manifest["calendar_binding"]["generation_id"] == market["bindings"]["calendar_binding"]["generation_id"]
    assert manifest["suspension_binding"]["dataset_generation_id"] == market["bindings"]["suspension_binding"]["dataset_generation_id"]
    assert manifest["corporate_action_binding"]["dataset_generation_id"] == market["bindings"]["corporate_action_binding"]["dataset_generation_id"]

    # and every market binding really resolves through its store
    market_store = BacktestMarketDatasetStore(tmp_path)
    market_store.read_frame(
        manifest["calendar_binding"]["generation_id"],
        manifest["calendar_binding"]["checksum_sha256"], dataset_type="calendar",
    )
    market_store.read_frame(
        manifest["suspension_binding"]["dataset_generation_id"],
        manifest["suspension_binding"]["data_checksum_sha256"],
        dataset_type="suspension", required_columns={"date", "instrument", "suspended"},
    )
    market_store.read_frame(
        manifest["corporate_action_binding"]["dataset_generation_id"],
        manifest["corporate_action_binding"]["data_checksum_sha256"],
        dataset_type="corporate_action", required_columns={"date", "instrument"},
    )
    AdjustedPriceDatasetStore(tmp_path).read_frame(price)


def test_the_cli_arguments_become_the_trading_parameters(tmp_path: Path) -> None:
    price = _adjusted_price(tmp_path, SESSIONS)
    market = _market_inputs(tmp_path)
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    _decisions_document(decisions, _reviewer_script(tmp_path / "reviewer.py"))

    report = _run(tmp_path, market, price, universe, decisions,
                  commission_bps=3.0, stamp_duty_bps=7.5, slippage_bps=2.0,
                  limit_ratio=0.2, initial_capital=2500000.0)
    manifest = BacktestConfigStore(tmp_path).read_manifest(report["generation_id"])
    assert manifest["cost_model"] == {"commission_bps": 3.0, "stamp_duty_bps": 7.5, "slippage_bps": 2.0}
    assert manifest["limit_rules"]["limit_ratio"] == 0.2
    assert manifest["initial_capital"] == 2500000.0
    # the execution model's consts are not caller-configurable
    assert manifest["execution_model"]["type"] == "daily_t1_open"
    assert manifest["execution_model"]["board_lot"] == 100
    assert manifest["execution_model"]["sellable_quantity_rule"] == "prior_day_holding_only"


def test_run_id_and_created_at_are_the_fixed_identity_neutral_values(tmp_path: Path) -> None:
    """cr.md 8.61: neither participates in this family's identity, so they must not
    be per-run values -- a second generation for one dataset would break the
    "this config belongs to this request" chain."""
    price = _adjusted_price(tmp_path, SESSIONS)
    market = _market_inputs(tmp_path)
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    _decisions_document(decisions, _reviewer_script(tmp_path / "reviewer.py"))

    report = _run(tmp_path, market, price, universe, decisions)
    manifest = BacktestConfigStore(tmp_path).read_manifest(report["generation_id"])
    assert manifest["run_id"] == "00000000-0000-4000-8000-000000000000"
    assert manifest["created_at"] == "1970-01-01T00:00:00+00:00"


def test_republishing_the_identical_config_is_refused_not_silently_reused(tmp_path: Path) -> None:
    """Every writer here is immutable, and unlike the market-data families this one
    has no content-identical skip: a swap would need the store to detect it, so the
    caller is expected to reuse the reported generation."""
    price = _adjusted_price(tmp_path, SESSIONS)
    market = _market_inputs(tmp_path)
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    _decisions_document(decisions, _reviewer_script(tmp_path / "reviewer.py"))

    report = _run(tmp_path, market, price, universe, decisions)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run(tmp_path, market, price, universe, decisions)
    assert "already published" in exc.value.stderr
    assert report["generation_id"]


def test_a_market_binding_that_does_not_resolve_is_refused(tmp_path: Path) -> None:
    price = _adjusted_price(tmp_path, SESSIONS)
    market = _market_inputs(tmp_path)
    # a report whose checksum does not match any published frame
    market["bindings"]["calendar_binding"]["checksum_sha256"] = "9" * 64
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    _decisions_document(decisions, _reviewer_script(tmp_path / "reviewer.py"))
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run(tmp_path, market, price, universe, decisions)
    assert "tampered" in exc.value.stderr or "unpublished" in exc.value.stderr


def test_an_unresolved_adjusted_price_generation_is_refused(tmp_path: Path) -> None:
    market = _market_inputs(tmp_path)
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    _decisions_document(decisions, _reviewer_script(tmp_path / "reviewer.py"))
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run(tmp_path, market, "a" * 64, universe, decisions)
    assert "unpublished adjusted price dataset" in exc.value.stderr


def test_a_missing_quality_decision_is_refused(tmp_path: Path) -> None:
    price = _adjusted_price(tmp_path, SESSIONS)
    market = _market_inputs(tmp_path)
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"quality_decisions": {}}), encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run(tmp_path, market, price, universe, decisions)
    # neither a wrapped document without the family, nor a bare object, is a review
    assert "no entry for backtest_config" in exc.value.stderr


def test_a_market_inputs_report_without_a_binding_is_refused(tmp_path: Path) -> None:
    price = _adjusted_price(tmp_path, SESSIONS)
    market = _market_inputs(tmp_path)
    market["bindings"].pop("suspension_binding")
    (tmp_path / "market-inputs.json").write_text(json.dumps(market), encoding="utf-8")
    universe = _universe_snapshot(tmp_path)
    decisions = tmp_path / "decisions.json"
    _decisions_document(decisions, _reviewer_script(tmp_path / "reviewer.py"))
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run(tmp_path, market, price, universe, decisions)
    assert "missing suspension_binding" in exc.value.stderr
