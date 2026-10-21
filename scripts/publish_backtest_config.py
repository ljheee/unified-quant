#!/usr/bin/env python3
"""Publish the `backtest_config.v1` artifact a research request binds.

`BacktestConfigStore.publish` derives the config's identity from its content and
binds an externally signed review, but nothing assembled the content, so a real
run had to hand-write one. This is that entry point: it resolves the four
bindings from already-published governed artifacts and the trading/cost/limit
parameters from explicit CLI arguments, then lets the store publish.

What it deliberately does NOT do: sign the quality review, or invent a binding.
`scripts/publish_feature_preprocessing.py` set the pattern -- a publication is
only meaningful if an independent reviewer attested it, so this script reads an
externally produced decision document and refuses to proceed without one. Every
binding value is either read from a named store or taken verbatim from the
command line; nothing is inferred from the filesystem.

The four bindings:

  * `universe_binding.snapshot_generation_id`     -- one published
    `universe_snapshot.v1` generation (a snapshot is PIT-scoped by
    `valid_from`/`valid_to`, so exactly one applies to a window).
  * `price_source_binding` -- the **partition set** of published
    `bars_adjusted/adjusted-v2` partitions inside `--start/--end`, as
    `{family, window_start, window_end, set_digest_sha256, member_count}`
    (cr.md 8.63).  The set digest is computed from the store's own enumeration, so
    the binding names exactly the partitions the backtest will read and
    concatenate -- no merged window artifact exists to drift.
  * `calendar_binding` / `suspension_binding` / `corporate_action_binding` --
    the three window-level artifacts published by
    `scripts/publish_backtest_market_data.py`.

Usage:
  python scripts/publish_backtest_config.py \
      --data-root data/research-root \
      --universe-root data/research-root \
      --universe-id csi-all --universe-snapshot <generation-id> \
      --market-inputs evidence/.../backtest-market-inputs.json \
      --start 2025-09-09 --end 2025-09-30 \
      --backtest-name research-baseline \
      --commission-bps 2.5 --stamp-duty-bps 5.0 --slippage-bps 1.0 \
      --limit-ratio 0.1 --initial-capital 1000000 \
      --quality-decisions-json evidence/.../quality-decisions.json \
      --out evidence/.../backtest-config-publication.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from uq.errors import ContractError
from uq.execution.set_binding_resolver import set_digest_sha256, verify_partition_set_binding
from uq.research_chain.owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    BacktestMarketDatasetStore,
)

PRODUCER = "scripts/publish_backtest_config.py"


def _load_decision(path: Path, family: str) -> Any:
    """Read the external review for `family` from a decisions document.

    Two shapes are accepted, because the family's subject is derived *inside* the
    store (cr.md U9) and therefore cannot be pre-signed by a static document:

    * `{"provider_command": [...]}` -- an external reviewer process invoked with
      the subject, exactly as `--decision-provider-json` drives the research CLI.
      This is the production shape: the reviewer signs for whichever provisional
      generation the store derives.
    * a decisions document holding a plain decision, as
      `scripts/publish_feature_preprocessing.py` accepts.  It only works when the
      caller already knows the subject, so a subject mismatch fails closed at the
      bind step rather than silently re-signing.

    The script never signs its own review in either case.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable quality decisions {path}: {exc}")
    if isinstance(document, dict) and "provider_command" in document:
        command = document["provider_command"]
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise SystemExit("provider_command must be a non-empty list of strings")
        if document.get("provider_id") != "external-model-quality-reviewer-v1":
            raise SystemExit("unsupported decision provider identity")
        from uq.research_chain.cli import _CommandDecisionProvider, _decision_provider_for_family

        return _decision_provider_for_family(
            _CommandDecisionProvider(
                command=command,
                provider_config_ref=document.get("provider_config_ref", "backtest-config-publisher"),
            ),
            family,
        )
    if isinstance(document, dict) and "quality_decisions" in document:
        decisions = document["quality_decisions"]
        if not isinstance(decisions, dict):
            raise SystemExit("quality_decisions must be an object")
        decision = decisions.get(family)
        if decision is None:
            raise SystemExit(f"quality decisions document has no entry for {family}")
        return decision
    if isinstance(document, dict) and document.get("binding_type") == f"{family}_v1":
        return document
    raise SystemExit(
        f"quality decisions {path} is neither a provider command nor a {family} decision"
    )


def _market_bindings(path: Path) -> dict[str, dict[str, str]]:
    """Read the three window-level bindings the market-input publisher emitted."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable market inputs {path}: {exc}")
    bindings = report.get("bindings")
    if not isinstance(bindings, dict):
        raise SystemExit(f"market inputs {path} carries no bindings object")
    for name in ("calendar_binding", "suspension_binding", "corporate_action_binding"):
        if not isinstance(bindings.get(name), dict):
            raise SystemExit(f"market inputs {path} is missing {name}")
    return bindings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True,
                        help="store root holding backtest_configs/ and canonical/")
    parser.add_argument("--universe-root", type=Path, required=True,
                        help="store root holding universes/")
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--universe-snapshot", required=True,
                        help="the published universe_snapshot.v1 generation for this window")
    parser.add_argument("--market-inputs", type=Path, required=True,
                        help="JSON report from scripts/publish_backtest_market_data.py")
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--backtest-name", required=True)
    parser.add_argument("--commission-bps", type=float, default=2.5)
    parser.add_argument("--stamp-duty-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--volume-participation-cap", type=float, default=None)
    parser.add_argument("--limit-ratio", type=float, default=0.1)
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--quality-decisions-json", type=Path, required=True,
                        help="externally signed decisions; the script never signs its own review")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    market = _market_bindings(args.market_inputs)

    # Every binding is read from a named store or taken from the command line; a
    # value that cannot be resolved is a hard stop rather than a guess.
    # Reading the frame through its store re-verifies the parquet bytes against the
    # manifest's checksum, so a name that does not resolve -- or a partition whose
    # bytes drifted -- is refused here rather than baked into the config.
    price_store = AdjustedPriceDatasetStore(args.data_root)
    price_triples = [
        triple for triple in price_store.list_partitions()
        if args.start.isoformat() <= str(triple["session_or_decision_date"]) <= args.end.isoformat()
    ]
    if not price_triples:
        raise SystemExit(
            f"no published adjusted-v2 partitions in {args.start}..{args.end}; "
            "run scripts/publish_adjusted_window.py first"
        )
    # cr.md 8.62: the backtest derives its calendar from the price panel and asserts
    # `set(price_dates) == set(calendar_dates)`, so a binding that does not cover the
    # whole window fails at the backtest stage with a message that names neither the
    # window nor the binding.  Refuse here, where the operator can still fix it.
    price_frame = price_store.read_partition_frames(price_triples)
    import pandas as pd

    session_column = "session_date" if "session_date" in price_frame.columns else "datetime"
    price_dates = sorted(
        pd.to_datetime(price_frame[session_column]).dt.strftime("%Y-%m-%d").unique().tolist()
    )
    if not price_dates:
        raise SystemExit("adjusted price frame names no session")
    outside = [d for d in price_dates if not (args.start.isoformat() <= d <= args.end.isoformat())]
    if outside:
        raise SystemExit(
            "adjusted price frame covers sessions outside the requested window: "
            + ", ".join(outside[:5])
        )
    price_source_binding = {
        "family": "bars_adjusted_adjusted_v2",
        "window_start": args.start.isoformat(),
        "window_end": args.end.isoformat(),
        "set_digest_sha256": set_digest_sha256(price_triples),
        "member_count": len(price_triples),
    }

    market_store = BacktestMarketDatasetStore(args.data_root)
    for name, dataset_type in (
        ("calendar_binding", "calendar"),
        ("suspension_binding", "suspension"),
        ("corporate_action_binding", "corporate_action"),
    ):
        binding = market[name]
        generation = binding.get("dataset_generation_id") or binding.get("generation_id")
        checksum = binding.get("data_checksum_sha256") or binding.get("checksum_sha256")
        # Reading through the store re-verifies the bytes against the manifest,
        # so a market-inputs report that does not match the store is refused here
        # rather than being baked into the config.
        market_store.read_frame(generation, checksum, dataset_type=dataset_type)

    manifest = {
        # cr.md 8.63: v2 names the window's partition set instead of one
        # partition, because the per-session line's invariants are between
        # adjacent partitions and must not be merged away.
        "contract_version": 2,
        "schema_version": "1.0.0",
        "backtest_name": args.backtest_name,
        "start_date": args.start.isoformat(),
        "end_date": args.end.isoformat(),
        "execution_model": {
            "type": "daily_t1_open",
            "board_lot": 100,
            "sellable_quantity_rule": "prior_day_holding_only",
            **(
                {} if args.volume_participation_cap is None
                else {"volume_participation_cap": args.volume_participation_cap}
            ),
        },
        "cost_model": {
            "commission_bps": args.commission_bps,
            "stamp_duty_bps": args.stamp_duty_bps,
            "slippage_bps": args.slippage_bps,
        },
        "limit_rules": {
            "limit_ratio": args.limit_ratio,
            "prev_close_source": "raw_prev_close",
            "adjustment_basis": "raw",
        },
        "calendar_binding": {
            "generation_id": market["calendar_binding"]["generation_id"],
            "checksum_sha256": market["calendar_binding"]["checksum_sha256"],
        },
        "price_source_binding": price_source_binding,
        "universe_binding": {"snapshot_generation_id": args.universe_snapshot},
        "initial_capital": args.initial_capital,
        # cr.md 8.61: neither run_id nor created_at participates in this family's
        # identity, so they are the fixed zero/epoch values the store and every
        # existing publisher already use -- a per-run value would mint a second
        # generation for one dataset and break "this config belongs to this
        # request".
        "run_id": "00000000-0000-4000-8000-000000000000",
        "created_at": "1970-01-01T00:00:00+00:00",
        "corporate_action_binding": {
            "dataset_generation_id": market["corporate_action_binding"]["dataset_generation_id"],
            "data_checksum_sha256": market["corporate_action_binding"]["data_checksum_sha256"],
        },
        "suspension_binding": {
            "dataset_generation_id": market["suspension_binding"]["dataset_generation_id"],
            "data_checksum_sha256": market["suspension_binding"]["data_checksum_sha256"],
        },
    }
    published = BacktestConfigStore(args.data_root).publish(
        manifest,
        quality_decision=_load_decision(
            args.quality_decisions_json, "backtest_config"
        ),
    )
    report = {
        "producer": PRODUCER,
        "generation_id": published["generation_id"],
        "manifest_digest_sha256": published["manifest_digest_sha256"],
        "request_binding": {
            "family": "backtest_config",
            "generation_id": published["generation_id"],
            "manifest_digest_sha256": published["manifest_digest_sha256"],
        },
        "window": {"start_date": args.start.isoformat(), "end_date": args.end.isoformat()},
        "universe_id": args.universe_id,
        "universe_snapshot_generation_id": args.universe_snapshot,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
