"""Regression tests for the 2026-10-10 adversarial review of commit 35c4e1b.

Each test names the finding it locks (U-numbering from
docs/adversarial/2026-10-10-cr35c4e1b-regression-review/REPORT.md).
"""

import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uq.contracts.adjustment_snapshot import (
    CONSUMABLE_FORMULA_VERSION,
    AdjustmentSnapshotReader,
)
from uq.contracts.config import load_dataset_contract
from uq.contracts.gate_contracts import adjustment_snapshot_generation, validate_contract
from uq.contracts.schema import ContractError, load_schema
from uq.errors import ContractError as StoreContractError
from uq.factors.raw_price import logical_fingerprint, verify_logical_fingerprint
from uq.market.adjustment import XdxrAdjustmentDeriver
from uq.pipeline.daily import DailyIngestPipeline
from uq.quality.gate import CrossSourceGate
from uq.routing.router import SourceRouter
from tests.test_adjustment_snapshot_runtime import _snapshot as save_snapshot
from tests.test_real_chain import FakeMootdxAdapter, mootdx_page

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config/schemas/bars_daily.research-v1.yaml"
CONTRACT_PATH = ROOT / "config/datasets/bars_daily.research-v1.yaml"


def _default_pipeline(tmp_path: Path, **overrides) -> DailyIngestPipeline:
    contract = load_dataset_contract(CONTRACT_PATH, load_schema(SCHEMA_PATH))
    options = {"run_health_probe": False}
    options.update(overrides)
    return DailyIngestPipeline(
        tmp_path,
        SCHEMA_PATH,
        CONTRACT_PATH,
        SourceRouter(contract, {"tdx": FakeMootdxAdapter(mootdx_page())}),
        **options,
    )


def test_default_tdx_pipeline_publishes_without_comparator(tmp_path):
    # U1 (BLOCKER): the documented default `uq-ingest daily` (no
    # --cross-validate) was rejected on every run — the gate received the
    # contract's tushare comparison rules with no tushare source present and
    # the comparator-unavailable error fired unconditionally.
    report = _default_pipeline(tmp_path).run(date(2026, 8, 21), ["600000.XSHG"])
    assert report.status == "published", report.errors
    assert report.rows == 1


def test_requested_cross_validation_stays_fail_closed(tmp_path):
    # U1 fix must not reopen H2: with cross-validation explicitly requested
    # and the comparator source absent, the run still fails closed.
    report = _default_pipeline(tmp_path, require_cross_validation=True).run(
        date(2026, 8, 21), ["600000.XSHG"]
    )
    assert report.status == "cross_validation_missing"
    assert any("tushare" in error for error in report.errors)


def _gate_frames(close_p=10.1, close_s=10.1, vol_p=100.0, vol_s=100.0):
    primary = pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "close": [close_p], "volume": [vol_p],
    })
    secondary = pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "close": [close_s], "volume": [vol_s],
    })
    rules = {
        "close": {"compare_with": "tushare", "max_abs_diff_pct": 0.001},
        "volume": {"compare_with": "tushare", "max_rel_diff_pct": 0.01},
    }
    return primary, secondary, rules


def test_gate_nonfinite_primary_is_a_conflict_both_branches():
    # U11: |primary| == inf used to be silently exempt in both the relative
    # branch (x/inf -> 0) and the absolute branch (x > inf*tol never true).
    primary, secondary, rules = _gate_frames(close_p=np.inf, close_s=10.0, vol_p=np.inf, vol_s=50.0)
    report = CrossSourceGate("tdx", rules).merge(
        {"tdx": primary, "tushare": secondary}, ["instrument", "datetime"]
    )
    assert not report.accepted
    assert {c["field"] for c in report.conflicts} == {"close", "volume"}


def test_gate_equal_infinities_stay_clean():
    # U11 must not over-reach: inf vs inf is not a divergence.
    primary, secondary, rules = _gate_frames(close_p=np.inf, close_s=np.inf, vol_p=np.inf, vol_s=np.inf)
    report = CrossSourceGate("tdx", rules).merge(
        {"tdx": primary, "tushare": secondary}, ["instrument", "datetime"]
    )
    assert not report.conflicts
    assert report.accepted


def test_contract_minimum_coverage_parsed_and_validated(tmp_path):
    # U12: minimum_coverage used to have no contract key and no production
    # constructor passing it, making the gate's coverage check unreachable.
    contract = load_dataset_contract(CONTRACT_PATH, load_schema(SCHEMA_PATH))
    assert contract.minimum_coverage == 0.9

    payload = CONTRACT_PATH.read_text(encoding="utf-8").replace(
        "minimum_coverage: 0.9", "minimum_coverage: 1.5"
    )
    bad = tmp_path / "bad-contract.yaml"
    bad.write_text(payload, encoding="utf-8")
    with pytest.raises(ContractError, match="minimum_coverage"):
        load_dataset_contract(bad, load_schema(SCHEMA_PATH))


def test_default_contract_minimum_coverage_is_zero(tmp_path):
    payload = CONTRACT_PATH.read_text(encoding="utf-8").replace(
        "minimum_coverage: 0.9\n", ""
    )
    path = tmp_path / "no-coverage-contract.yaml"
    path.write_text(payload, encoding="utf-8")
    contract = load_dataset_contract(path, load_schema(SCHEMA_PATH))
    assert contract.minimum_coverage == 0.0


def _factor_frame(value: float) -> pd.DataFrame:
    return pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "volume_ratio_20d": [value],
    })


def test_fingerprint_distinguishes_signed_infinities():
    # U18/U24: the H11 algorithm sampled the null mask *before* mapping both
    # infinities to NaN, so +inf and -inf still collided with each other.
    assert logical_fingerprint(_factor_frame(np.inf)) != logical_fingerprint(_factor_frame(-np.inf))
    assert logical_fingerprint(_factor_frame(np.inf)) != logical_fingerprint(_factor_frame(np.nan))
    assert logical_fingerprint(_factor_frame(np.inf)) == logical_fingerprint(_factor_frame(np.inf))


def test_fingerprint_read_path_accepts_all_known_versions():
    # U2 migration: partitions/manifests pinned by the legacy (v1, fillna) or
    # H11 (v2, null-mask) algorithms must stay readable; writes use the
    # latest version. Tampered content must fail every version.
    frame = _factor_frame(1.5)
    legacy_pin = logical_fingerprint(frame, version=1)
    h11_pin = logical_fingerprint(frame, version=2)
    latest_pin = logical_fingerprint(frame)
    assert verify_logical_fingerprint(frame, legacy_pin)
    assert verify_logical_fingerprint(frame, h11_pin)
    assert verify_logical_fingerprint(frame, latest_pin)
    assert not verify_logical_fingerprint(_factor_frame(2.5), legacy_pin)
    assert not verify_logical_fingerprint(_factor_frame(2.5), h11_pin)
    assert not verify_logical_fingerprint(frame, None)
    assert not verify_logical_fingerprint(frame, "")


def _craft_legacy_v1_snapshot(tmp_path: Path) -> str:
    """Simulate a pre-flip v1 snapshot: identical raw event CSVs, v1 manifest."""
    directory = save_snapshot(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "generation_id"}
    unsigned["snapshot_version"] = 1
    unsigned["formula_version"] = "adj_factor.exchange_v1"
    generation_id = adjustment_snapshot_generation(unsigned)
    legacy = directory.with_name(generation_id)
    directory.rename(legacy)
    payload = {**unsigned, "generation_id": generation_id}
    (legacy / "manifest.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return generation_id


def test_snapshot_store_writes_v2_and_reader_roundtrips(tmp_path):
    # U3: the in-place const flip is rolled back into proper versioning —
    # writes now carry snapshot_version 2 under the new v2 schema, and the
    # reader dispatches validation by the manifest's own version marker.
    directory = save_snapshot(tmp_path)
    reader = AdjustmentSnapshotReader(tmp_path)
    manifest = reader.read_manifest("600000.XSHG", directory.name)
    assert manifest["snapshot_version"] == 2
    assert manifest["formula_version"] == "adj_factor.exchange_v2"
    validate_contract("adjustment_snapshot.v2.json", manifest)
    selected = reader.select_visible_generation(
        instrument="600000.XSHG",
        window_start=date(2026, 8, 20),
        window_end=date(2026, 8, 21),
        as_of_time=datetime.fromisoformat("2026-08-21T08:00:00+00:00"),
        candidates=[directory.name],
    )
    assert selected == directory.name


def test_legacy_v1_snapshot_stays_readable_but_is_not_consumable(tmp_path):
    # U3: stored v1 manifests must still validate on read — the in-place const
    # flip broke exactly that. U6/U17: but the consumption seam rejects the v1
    # lineage with re-export guidance instead of silently consuming it under
    # the v2 multiply basis.
    generation_id = _craft_legacy_v1_snapshot(tmp_path)
    reader = AdjustmentSnapshotReader(tmp_path)
    manifest = reader.read_manifest("600000.XSHG", generation_id)
    assert manifest["snapshot_version"] == 1
    assert manifest["formula_version"] == "adj_factor.exchange_v1"
    assert reader.read_events("600000.XSHG", generation_id).shape[0] == 1
    with pytest.raises(StoreContractError, match="not consumable"):
        reader.select_visible_generation(
            instrument="600000.XSHG",
            window_start=date(2026, 8, 20),
            window_end=date(2026, 8, 21),
            as_of_time=datetime.fromisoformat("2026-08-21T08:00:00+00:00"),
            candidates=[generation_id],
        )


def test_unknown_snapshot_version_is_rejected(tmp_path):
    # U3 dispatch must fail closed on versions it does not know.
    generation_id = _craft_legacy_v1_snapshot(tmp_path)
    path = tmp_path / "adjustments" / "snapshots" / "600000.XSHG" / generation_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["snapshot_version"] = 3
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StoreContractError, match="unsupported adjustment snapshot version"):
        AdjustmentSnapshotReader(tmp_path).read_manifest("600000.XSHG", generation_id)


def test_v1_snapshot_reexports_through_current_deriver(tmp_path):
    # The migration path the lineage gate's error message points to: a v1
    # document's raw events re-derived under the current deriver yield the v2
    # lineage — no event data is lost in the version cut.
    generation_id = _craft_legacy_v1_snapshot(tmp_path)
    reader = AdjustmentSnapshotReader(tmp_path)
    events = reader.read_events("600000.XSHG", generation_id)
    sessions = pd.to_datetime(["2026-08-20", "2026-08-21"])
    pre_close = pd.Series([10.0, 10.0], index=sessions)
    derivation = XdxrAdjustmentDeriver().derive("600000.XSHG", events, sessions, pre_close)
    assert derivation.version == CONSUMABLE_FORMULA_VERSION
    assert len(derivation.frame) == 2


def test_lineage_constant_is_single_sourced():
    # U6/U17: the factor layer's _LINEAGE_VERSION was a dead duplicate of the
    # deriver version string; it must alias the enforced gate constant so the
    # two layers cannot drift apart again.
    from uq.factors.adjusted_price import _LINEAGE_VERSION

    assert _LINEAGE_VERSION == CONSUMABLE_FORMULA_VERSION


def _quality_decision(subject: str, **overrides):
    from uq.contracts.model_layer import create_reviewed_quality_decision
    from tests.review_key import REVIEWER_PRIVATE_KEY

    kwargs = dict(
        binding_type="model_artifact_v1", policy="reject_all", status="passed",
        checks=[{"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
        subject_generation_id=subject,
    )
    kwargs.update(overrides)
    return create_reviewed_quality_decision(**kwargs)


def test_quality_decision_rejects_cross_subject_replay():
    # U9: the H5 signed field set did not cover the subject, so a decision
    # signed for subject A could be attached to subject B at execute time.
    # The signature now commits to the subject; binding to B must fail.
    from uq.contracts.model_layer import bind_reviewed_quality_decision

    decision = _quality_decision("a" * 64)
    report, _ = bind_reviewed_quality_decision(
        decision, binding_type="model_artifact_v1", subject_generation_id="a" * 64
    )
    assert report["bound_generation_id"] == "a" * 64
    with pytest.raises(StoreContractError, match="different subject generation"):
        bind_reviewed_quality_decision(
            decision, binding_type="model_artifact_v1", subject_generation_id="b" * 64
        )
    with pytest.raises(StoreContractError, match="different subject content"):
        bind_reviewed_quality_decision(
            decision, binding_type="model_artifact_v1",
            subject_generation_id="a" * 64, subject_content_sha256="c" * 64,
        )


def test_subject_less_legacy_decision_fails_closed_at_bind():
    # U9: decisions minted before the subject entered the signed field set
    # (11 fields) must be rejected at execute time, not grandfathered.
    from uq.contracts.model_layer import bind_reviewed_quality_decision

    decision = _quality_decision("a" * 64)
    legacy = {key: value for key, value in decision.items() if not key.startswith("subject_")}
    with pytest.raises(StoreContractError, match="unexpected or missing fields"):
        bind_reviewed_quality_decision(
            legacy, binding_type="model_artifact_v1", subject_generation_id="a" * 64
        )


def test_bound_report_signature_verification_covers_subject():
    # U9: stored-report re-verification reconstructs the signed subject from
    # bound_generation_id/subject_content_sha256; retargeting the report to
    # another subject breaks the signature even though those fields sit
    # outside the decision object.
    from uq.contracts.model_layer import (
        bind_reviewed_quality_decision, verify_reviewed_quality_report_signature,
    )

    decision = _quality_decision("a" * 64)
    report, _ = bind_reviewed_quality_decision(
        decision, binding_type="model_artifact_v1", subject_generation_id="a" * 64
    )
    verify_reviewed_quality_report_signature(report)
    retargeted = {**report, "bound_generation_id": "b" * 64}
    with pytest.raises(StoreContractError):
        verify_reviewed_quality_report_signature(retargeted)


def test_risk_industry_rule_requires_policy_bound_artifact():
    # U10: industry_mapping used to be an unverified caller input, so a caller
    # could relabel a concentrated book into harmless industries. The
    # aggregation now runs only on the membership artifact the policy binds.
    from tests.test_risk_control_plane_phase1 import (
        _evaluate,
        _industry_membership,
        _industry_policy,
    )

    artifact = _industry_membership({"A": "tech", "B": "finance"})
    policy = _industry_policy(threshold=0.9, artifact=artifact)

    # A bare caller mapping is no longer accepted when an industry rule runs.
    with pytest.raises(StoreContractError, match="artifact is required"):
        _evaluate(policy=policy, industry_mapping={"A": "tech", "B": "finance"})

    # A self-consistent artifact the policy does not bind is rejected.
    forged = _industry_membership({"A": "cash", "B": "cash"})
    with pytest.raises(StoreContractError, match="binding generation mismatch"):
        _evaluate(policy=policy, industry_membership=forged)

    # A bound artifact whose mapping was tampered after signing is rejected.
    tampered = {**artifact, "mapping": {"A": "cash", "B": "cash"}}
    with pytest.raises(StoreContractError, match="identity mismatch"):
        _evaluate(policy=policy, industry_membership=tampered)

    # A caller mapping that disagrees with the bound artifact fails loudly
    # instead of being silently ignored.
    with pytest.raises(StoreContractError, match="conflicts"):
        _evaluate(
            policy=policy,
            industry_membership=artifact,
            industry_mapping={"A": "cash", "B": "cash"},
        )

    # The bound artifact itself aggregates and passes under a loose limit.
    decision = _evaluate(policy=policy, industry_membership=artifact)
    industry_finding = next(
        item for item in decision["findings"] if item["rule_id"] == "portfolio_industry_limit"
    )
    assert industry_finding["observed"] == pytest.approx(0.6)
    assert industry_finding["result"] == "passed"


def test_resolve_subject_quality_decision_is_arity_adaptive():
    # U14: the research-chain runner's legacy one-arg providers keep working at
    # eagerly resolved sites, while flows whose subject content digest differs
    # from the generation id resolve two-arg providers per subject.
    from uq.contracts.model_layer import resolve_subject_quality_decision

    one_arg = lambda generation_id: ("one", generation_id)  # noqa: E731
    two_arg = lambda generation_id, content_sha256: ("two", generation_id, content_sha256)  # noqa: E731
    assert resolve_subject_quality_decision(
        one_arg, subject_generation_id="g", subject_content_sha256="c"
    ) == ("one", "g")
    assert resolve_subject_quality_decision(
        two_arg, subject_generation_id="g", subject_content_sha256="c"
    ) == ("two", "g", "c")
    decision = {"status": "passed"}
    assert resolve_subject_quality_decision(
        decision, subject_generation_id="g", subject_content_sha256="c"
    ) is decision


def _quality_envelope(decision, *, subject_generation_id, subject_content_sha256=None):
    """Wrap a raw decision in a resolver-style quality_decision.v1 envelope."""
    from uq.contracts.model_layer import bind_reviewed_quality_decision

    report, checksum = bind_reviewed_quality_decision(
        decision,
        binding_type=decision["binding_type"],
        subject_generation_id=subject_generation_id,
        subject_content_sha256=subject_content_sha256,
    )
    return {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "binding_type": decision["binding_type"],
        "subject_generation_id": subject_generation_id,
        "subject_manifest_digest_sha256": report["subject_content_sha256"],
        "owning_report": report,
        "decision_checksum_sha256": checksum,
        "provider_id": "test-provider",
        "trust_anchor_id": report["key_id"],
    }


def test_bind_adopts_verified_resolver_envelope():
    # U15: production hands stages quality_decision.v1 envelopes (resolver
    # lookup shape) while tests/providers hand raw signed decisions — bind
    # used to crash on the envelope field set. The envelope is adopted only
    # after full checksum/signature/subject verification.
    from uq.contracts.model_layer import bind_reviewed_quality_decision

    decision = _quality_decision("a" * 64)
    envelope = _quality_envelope(decision, subject_generation_id="a" * 64)

    report, checksum = bind_reviewed_quality_decision(
        envelope, binding_type="model_artifact_v1", subject_generation_id="a" * 64
    )
    assert report["bound_generation_id"] == "a" * 64
    assert checksum == envelope["decision_checksum_sha256"]

    with pytest.raises(StoreContractError, match="different subject generation"):
        bind_reviewed_quality_decision(
            envelope, binding_type="model_artifact_v1", subject_generation_id="b" * 64
        )
    with pytest.raises(StoreContractError, match="does not match"):
        bind_reviewed_quality_decision(
            envelope, binding_type="model_run_v1", subject_generation_id="a" * 64
        )
    with pytest.raises(StoreContractError, match="different subject content"):
        bind_reviewed_quality_decision(
            envelope,
            binding_type="model_artifact_v1",
            subject_generation_id="a" * 64,
            subject_content_sha256="c" * 64,
        )
    # A tampered owning report breaks the envelope's own checksum/signature
    # verification inside the quality_decision contract validation.
    tampered = {**envelope, "owning_report": {**envelope["owning_report"], "status": "rejected"}}
    with pytest.raises(StoreContractError):
        bind_reviewed_quality_decision(
            tampered, binding_type="model_artifact_v1", subject_generation_id="a" * 64
        )


def _split_manifest(splits):
    return {"split_policy": {"splits": splits}}


def test_qlib_train_split_validation_is_pure_and_rejects():
    # U20: the C2 train-split validation used to live inline in
    # QlibRuntimeTrainer (qlib-gated module), so its rejection paths never ran
    # in qlib-free environments. The pure function locks them without qlib.
    from uq.models.dataset import resolve_train_split

    calendar = ["2026-01-05", "2026-01-06", "2026-01-07"]
    good = _split_manifest([
        {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-06"},
        {"name": "validation", "start_date": "2026-01-07", "end_date": "2026-01-07"},
    ])
    assert resolve_train_split(good, calendar) == ("2026-01-05", "2026-01-06")

    no_train = _split_manifest([
        {"name": "validation", "start_date": "2026-01-05", "end_date": "2026-01-06"},
    ])
    with pytest.raises(StoreContractError, match="exactly one train split"):
        resolve_train_split(no_train, calendar)

    two_trains = _split_manifest([
        {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-06"},
        {"name": "train", "start_date": "2026-01-06", "end_date": "2026-01-07"},
    ])
    with pytest.raises(StoreContractError, match="exactly one train split"):
        resolve_train_split(two_trains, calendar)

    early_start = _split_manifest([
        {"name": "train", "start_date": "2026-01-04", "end_date": "2026-01-06"},
    ])
    with pytest.raises(StoreContractError, match="does not cover"):
        resolve_train_split(early_start, calendar)

    late_end = _split_manifest([
        {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-08"},
    ])
    with pytest.raises(StoreContractError, match="does not cover"):
        resolve_train_split(late_end, calendar)


def test_universe_id_schema_pattern_matches_runtime_whitelist():
    # U4: the schema accepted universe ids the H12 runtime whitelist rejects
    # (single-char, uppercase, dotted) — schema and runtime now agree.
    from tests.test_gate_exit_contracts import _universe_valid

    base = _universe_valid()
    validate_contract("universe_snapshot.v1.json", dict(base))
    for bad_id in ("a", "Research", "research.cn"):
        with pytest.raises(StoreContractError):
            validate_contract("universe_snapshot.v1.json", {**base, "universe_id": bad_id})


def test_accepted_runtime_reads_through_injected_registry(tmp_path):
    # U16: AcceptedFactorIndexRuntime used to rebuild a registry from the
    # source tree on every read, reopening the publish/accept registry
    # divergence window that H10 closed on the FactorStore side.
    from tests.test_accepted_store_runtime import _publish_factor
    from uq.contracts.factor_governance import FactorRegistry
    from uq.models.accepted_store import AcceptedFactorIndexRuntime

    _publish_factor(tmp_path)
    query = {
        "contract_version": 1,
        "filters": {},
        "ordering": ["partition_date"],
        "visibility": "accepted_only",
        "pagination": {"limit": 10},
    }
    # A registry without factor definitions must drive the reads; if the
    # runtime rebuilt its own registry from the source tree this would pass.
    empty_registry = FactorRegistry(tmp_path / "empty-root")
    with pytest.raises(StoreContractError, match="tampered or invalid"):
        AcceptedFactorIndexRuntime(tmp_path, registry=empty_registry).list(query)

    registry = FactorRegistry(ROOT)
    runtime = AcceptedFactorIndexRuntime(tmp_path, registry=registry)
    entries = runtime.list(query)
    assert len(entries) == 1
    frame = runtime.read(entries[0]["generation_id"])
    assert "close_location_1d" in frame.columns


def test_execution_config_initial_state_provenance_commits_to_content():
    # U13: initial_state.provenance_sha256 was existence-only — any well-formed
    # 64-hex string passed and the only downstream check compared it against
    # the operator's own claim. The provenance is now a canonical commitment
    # to the declared holdings/cash/valuation basis, recomputed and verified
    # whenever an execution_config is validated.
    from uq.contracts.model_layer import (
        ModelContractLoader,
        initial_state_provenance_sha256,
        paper_execution_identities,
    )

    fixture = ROOT / "config/schemas/fixtures/paper-execution/execution_config-valid.json"
    config = json.loads(fixture.read_text())
    assert config["initial_state"]["provenance_sha256"] == initial_state_provenance_sha256(
        config["initial_state"]
    )
    ModelContractLoader.validate("execution_config", dict(config))

    # A self-asserted opaque provenance is rejected even when the attacker
    # recomputes valid generation/manifest identities over the forged payload.
    forged = json.loads(fixture.read_text())
    forged["initial_state"]["provenance_sha256"] = "7" * 64
    forged["generation_id"], forged["manifest_digest_sha256"] = paper_execution_identities(
        forged, schema_name="execution_config"
    )
    with pytest.raises(StoreContractError, match="does not commit to the initial state content"):
        ModelContractLoader.validate("execution_config", forged)

    # A provenance that honestly commits to a DIFFERENT initial state is
    # equally rejected: the hash must match this config's declared content.
    foreign = json.loads(fixture.read_text())
    foreign["initial_state"]["provenance_sha256"] = initial_state_provenance_sha256(
        {"cash": 0.0, "holdings": [], "valuation_price_basis": "decision_close_estimate"}
    )
    foreign["generation_id"], foreign["manifest_digest_sha256"] = paper_execution_identities(
        foreign, schema_name="execution_config"
    )
    with pytest.raises(StoreContractError, match="does not commit to the initial state content"):
        ModelContractLoader.validate("execution_config", foreign)


def test_cli_decision_provider_forwards_subject_digest():
    # X1/U14: provider-json mode wraps the external provider in a callable.
    # The old one-argument wrapper could never complete an execute — the
    # runner fetched stage-owned decisions without a subject and raised
    # "quality decision provider requires a subject" before any digest
    # comparison. The wrapper is now two-argument and forwards the digest end
    # to end — which genuinely matters only where content differs from the
    # generation id (qlib export/receipt, target_weights); elsewhere
    # content==generation.
    from uq.research_chain.cli import _decision_provider_for_family

    calls = []

    class _FakeProvider:
        provider_config_ref = "research-cli"

        def resolve(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    resolve = _decision_provider_for_family(_FakeProvider(), "model_run")

    # Passthrough style (content≠generation stage flows): both subjects arrive.
    assert resolve("g" * 4, "c" * 4) == {"ok": True}
    assert calls[-1]["subject_generation_id"] == "g" * 4
    assert calls[-1]["subject_manifest_digest_sha256"] == "c" * 4
    assert calls[-1]["binding_type"] == "model_run_v1"

    # Eager/legacy style (content==generation flows): digest defaults to None,
    # which the provider treats as content==generation.
    assert resolve("g" * 4) == {"ok": True}
    assert calls[-1]["subject_manifest_digest_sha256"] is None


def test_cli_provider_wrapper_compose_with_runner_resolution():
    # X1/U14 end-to-end seam: the CLI wrapper composed with the runner's
    # arity-adaptive resolution delivers (generation, content) pairs correctly
    # for both flow classes — the composition that used to strand provider mode.
    from uq.contracts.model_layer import resolve_subject_quality_decision
    from uq.research_chain.cli import _decision_provider_for_family

    calls = []

    class _FakeProvider:
        provider_config_ref = "research-cli"

        def resolve(self, **kwargs):
            calls.append((kwargs["subject_generation_id"], kwargs["subject_manifest_digest_sha256"]))
            return {"ok": True}

    wrapper = _decision_provider_for_family(_FakeProvider(), "prediction_set")
    assert resolve_subject_quality_decision(
        wrapper, subject_generation_id="g" * 4, subject_content_sha256="c" * 4
    ) == {"ok": True}
    assert calls[-1] == ("g" * 4, "c" * 4)

    eager = _decision_provider_for_family(_FakeProvider(), "backtest_config")
    assert resolve_subject_quality_decision(
        eager, subject_generation_id="g" * 4, subject_content_sha256="g" * 4
    ) == {"ok": True}
    assert calls[-1] == ("g" * 4, "g" * 4)


def test_portfolio_definition_raw_decision_binds_with_manifest_digest():
    # 12d44af review follow-up (U15 residual): the runner synthesizes the
    # portfolio definition and now resolves providers with the true subject
    # content (the manifest digest), so an honest two-argument provider
    # returning a raw signed decision — the shape U15 legitimizes — binds
    # end to end. A one-argument provider's decision defaults its content
    # digest to the generation id and still fails closed at the raw branch
    # (envelope-only), matching the documented protocol.
    from uq.contracts.model_layer import (
        bind_reviewed_quality_decision,
        create_reviewed_quality_decision,
        model_manifest_identities,
    )
    from uq.portfolio.builder import PortfolioDefinitionBinding
    from uq.research_chain import ResearchChainRunner
    from tests.review_key import REVIEWER_PRIVATE_KEY

    fixture = json.loads(
        (ROOT / "evidence/portfolio-backtest/phase-0/fixtures/valid_portfolio_definition.json").read_text()
    )
    fixture["quality_report_checksum_sha256"] = "0" * 64
    fixture["generation_id"], fixture["manifest_digest_sha256"] = model_manifest_identities(
        {**fixture, "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
        schema_name="portfolio_definition",
    )
    checks = [
        {"name": "weight_scheme_valid", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        {"name": "constraints_within_bounds", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        {"name": "universe_binding_resolved", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
    ]

    def raw_provider(generation_id: str, content_sha256: str) -> dict:
        return create_reviewed_quality_decision(
            binding_type="portfolio_definition_v1", policy="reject_all", status="passed",
            checks=checks, errors=[], warnings=[], producer_code_fingerprint="0" * 64,
            private_key_pem=REVIEWER_PRIVATE_KEY,
            subject_generation_id=generation_id, subject_content_sha256=content_sha256,
        )

    # _decision carries no instance state — exercise the runner resolution
    # seam directly without wiring the full adapter set.
    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    decision = runner._decision(
        {"portfolio_definition": raw_provider}, "portfolio_definition",
        fixture["generation_id"], subject_content_sha256=fixture["manifest_digest_sha256"],
    )
    _, bound = PortfolioDefinitionBinding.bind(definition=dict(fixture), quality_decision=dict(decision))
    assert bound["bound_generation_id"] == fixture["generation_id"]
    assert bound["subject_content_sha256"] == fixture["manifest_digest_sha256"]

    def legacy_provider(generation_id: str) -> dict:
        return raw_provider(generation_id, generation_id)

    rejected = runner._decision(
        {"portfolio_definition": legacy_provider}, "portfolio_definition",
        fixture["generation_id"], subject_content_sha256=fixture["manifest_digest_sha256"],
    )
    with pytest.raises(StoreContractError, match="different subject content"):
        PortfolioDefinitionBinding.bind(definition=dict(fixture), quality_decision=dict(rejected))


def test_v1_state_publishes_at_v1_stage_number(tmp_path):
    # CI run 38053013828 regression lock: validate_research_layout hardcoded
    # the V3 ten-stage order, so a V1/V2 state at result_reconciliation was
    # numbered stage=08 by stage_plan_for_request but expected at stage=09
    # by the layout validator — a failed run's state publish raised
    # "research path does not match state layout" and masked the original
    # failure. The layout validator now takes the document's own stage plan.
    from uq.contracts.model_layer import (
        RESEARCH_STAGE_ORDER_V1,
        research_contract_identities,
    )
    from uq.research_chain.resolver import FileResearchRunStore

    run_id = "00000000-0000-4000-8000-000000000001"
    records = [
        {"stage": stage, "status": "passed", "output_bindings": [], "failure_reason": None}
        for stage in RESEARCH_STAGE_ORDER_V1[:-1]
    ]
    records.append({
        "stage": "result_reconciliation", "status": "failed",
        "output_bindings": [], "failure_reason": "stage_failed",
    })
    state = {
        "contract_version": 1, "schema_version": "1.0.0",
        "request_content_generation_id": "a" * 64,
        "request_manifest_digest_sha256": "b" * 64,
        "run_id": run_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "runner_identity": {
            "code_fingerprint": "c" * 64,
            "environment_profile": "locked-test",
            "lock_digest_sha256": "d" * 64,
        },
        "intent": "execute",
        "stage_records": records,
        "final_status": "failed",
        "state_content_generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    state["state_content_generation_id"], state["manifest_digest_sha256"] = (
        research_contract_identities(state, schema_name="research_run_state")
    )
    store = FileResearchRunStore(tmp_path)
    published = store.publish_state(state, stage="result_reconciliation")
    expected = (
        tmp_path / "research_runs" / "states" / f"request={'a' * 64}"
        / f"run={run_id}" / "stage=08" / "manifest.json"
    )
    assert published.manifest_path == expected
    assert published.manifest_path.is_file()
    reread = store.read_state(
        state["request_content_generation_id"], run_id,
        "result_reconciliation", state["manifest_digest_sha256"],
    )
    assert reread["stage_records"][-1]["status"] == "failed"


def test_research_layout_stage_number_follows_explicit_stage_order(tmp_path):
    # Locks the validate_research_layout stage_order parameter: the same
    # stage numbers differently under the V1 (runtime) and V3 (disabled
    # paper-execution) plans, and the validator must honor the caller's plan
    # instead of a hardcoded one; the version-blind default is the V1 plan.
    from uq.contracts.model_layer import RESEARCH_STAGE_ORDER_V1, RESEARCH_STAGE_ORDER_V3
    from uq.research_chain.contracts import validate_research_layout

    run_id = "00000000-0000-4000-8000-000000000001"
    for order, number in ((RESEARCH_STAGE_ORDER_V1, "08"), (RESEARCH_STAGE_ORDER_V3, "09")):
        path = (
            tmp_path / "research_runs" / "states" / f"request={'a' * 64}"
            / f"run={run_id}" / f"stage={number}" / "manifest.json"
        )
        path.parent.mkdir(parents=True)
        resolved = validate_research_layout(
            path, data_root=tmp_path, kind="state",
            request_generation_id="a" * 64, run_id=run_id,
            stage="result_reconciliation", stage_order=order,
        )
        assert resolved == path
        other = "09" if number == "08" else "08"
        wrong = (
            tmp_path / "research_runs" / "states" / f"request={'b' * 64}"
            / f"run={run_id}" / f"stage={other}" / "manifest.json"
        )
        wrong.parent.mkdir(parents=True)
        with pytest.raises(StoreContractError, match="does not match state layout"):
            validate_research_layout(
                wrong, data_root=tmp_path, kind="state",
                request_generation_id="b" * 64, run_id=run_id,
                stage="result_reconciliation", stage_order=order,
            )

    default_path = (
        tmp_path / "research_runs" / "states" / f"request={'c' * 64}"
        / f"run={run_id}" / "stage=08" / "manifest.json"
    )
    default_path.parent.mkdir(parents=True)
    assert validate_research_layout(
        default_path, data_root=tmp_path, kind="state",
        request_generation_id="c" * 64, run_id=run_id, stage="result_reconciliation",
    ) == default_path


def test_reject_all_registry_bindings_admit_only_passed_status(tmp_path):
    # Residual R1 (U22 follow-up): the reviewed registry used to declare
    # policy=reject_all while admitting ["passed", "warning"] on every
    # model-side binding, so a warning decision minted and bound — while
    # the factor and canonical sides reject anything but passed under the
    # same policy (factors/store.py:161/:560, contracts/canonical_v2.py:204).
    # The registry now unifies on reject_all ⇒ passed-only and refuses to
    # load a contradictory binding. feature_preprocessing_v1 keeps
    # passed+warning under its non-reject_all policy, matching the
    # factor-side policy-adaptive rule.
    from uq.contracts.model_layer import (
        ModelQualityReviewRegistry,
        create_reviewed_quality_decision,
    )
    from tests.review_key import REVIEWER_PRIVATE_KEY

    registry = ModelQualityReviewRegistry()
    reject_all = {
        bt: b for bt, b in registry.bindings.items() if b.get("policy") == "reject_all"
    }
    assert reject_all, "expected reject_all bindings in the reviewed registry"
    assert all(b["allowed_statuses"] == ["passed"] for b in reject_all.values())
    assert registry.bindings["feature_preprocessing_v1"]["allowed_statuses"] == [
        "passed", "warning",
    ]

    with pytest.raises(StoreContractError, match="not in allowed statuses"):
        create_reviewed_quality_decision(
            binding_type="model_artifact_v1", policy="reject_all", status="warning",
            checks=[], errors=[], warnings=[], producer_code_fingerprint="0" * 64,
            private_key_pem=REVIEWER_PRIVATE_KEY,
            subject_generation_id="0" * 64,
        )

    payload = json.loads(registry.path.read_text())
    payload["bindings"]["model_artifact_v1"]["allowed_statuses"] = ["passed", "warning"]
    contradictory = tmp_path / "registry.json"
    contradictory.write_text(json.dumps(payload, indent=2) + "\n")
    with pytest.raises(StoreContractError, match="must admit only passed"):
        ModelQualityReviewRegistry(contradictory)


def test_decision_provider_arity_is_strict():
    # Residual R4: resolve_subject_quality_decision used to route providers
    # by raw parameter count — a *args callable was invoked with one
    # argument and a three-parameter callable failed late at the call site,
    # while an uninspectable signature silently fell back to two arguments.
    # The contract is now strict: exactly one or two positional arguments,
    # inspectable signature, no *args, no required keyword-only parameters.
    from uq.contracts.model_layer import resolve_subject_quality_decision

    subject_g, subject_c = "g" * 64, "c" * 64
    assert resolve_subject_quality_decision(
        lambda g: ("one", g),
        subject_generation_id=subject_g, subject_content_sha256=subject_c,
    ) == ("one", subject_g)
    assert resolve_subject_quality_decision(
        lambda g, c: ("two", g, c),
        subject_generation_id=subject_g, subject_content_sha256=subject_c,
    ) == ("two", subject_g, subject_c)
    # an optional second parameter still receives the content digest
    assert resolve_subject_quality_decision(
        lambda g, c=None: ("opt", c),
        subject_generation_id=subject_g, subject_content_sha256=subject_c,
    ) == ("opt", subject_c)
    for bad in (
        lambda *args: None,
        lambda g, c, extra: None,
        lambda: None,
        lambda g, *, c: None,
    ):
        with pytest.raises(StoreContractError, match="exactly one or two positional"):
            resolve_subject_quality_decision(
                bad, subject_generation_id=subject_g, subject_content_sha256=subject_c,
            )
    # static decisions pass through untouched
    assert resolve_subject_quality_decision(
        {"static": True}, subject_generation_id=subject_g, subject_content_sha256=subject_c,
    ) == {"static": True}


def test_pipeline_builders_lock_their_wiring(tmp_path):
    # Residual R3 (U1 follow-up): U1's regression test rebuilt the pipeline
    # by hand from the real contract, so the builders themselves were never
    # locked — a wiring change (dropping the tushare adapter, the calendar,
    # or the cross-validation flags) would have passed silently.
    from uq.pipeline.daily import build_tdx_pipeline, build_tushare_crosscheck_pipeline

    tdx = build_tdx_pipeline(tmp_path / "tdx", ROOT, run_health_probe=False)
    assert set(tdx.router.adapters) == {"tdx"}
    assert tdx.router.calendar is None
    assert tdx.require_cross_validation is False
    assert tdx.require_complete_route is False
    assert tdx.run_health_probe is False

    cross = build_tushare_crosscheck_pipeline(tmp_path / "cross", ROOT, run_health_probe=False)
    assert set(cross.router.adapters) == {"tdx", "tushare"}
    assert cross.router.calendar is not None
    assert cross.router.calendar.provenance == "akshare_sina_trade_dates"
    assert cross.require_cross_validation is True
    assert cross.require_complete_route is True
    assert cross.verified_rows_only is True

    # both builders load the same frozen research contract the U1 test uses
    assert tdx.contract.cross_validation == cross.contract.cross_validation
    assert tdx.contract.minimum_coverage == cross.contract.minimum_coverage


def test_cross_validation_mode_is_declared_and_enforced(tmp_path):
    # Residual R2 (U1 semantic debt): the research-v1 contract declared
    # cross_validation rules + minimum_coverage: 0.9 while the default
    # ingest path never executed them, so every reviewer read the
    # declaration as always-enforced. The contract now declares its mode
    # explicitly; "required" makes a pipeline that skips cross-validation
    # fail at construction instead of silently under-enforcing.
    from uq.contracts.config import load_dataset_contract

    contract = load_dataset_contract(CONTRACT_PATH, load_schema(SCHEMA_PATH))
    assert contract.cross_validation_mode == "optional"

    # optional mode keeps the default TDX-only path legal (U1 stays closed)
    report = _default_pipeline(tmp_path / "opt").run(date(2026, 8, 21), ["600000.XSHG"])
    assert report.status == "published", report.errors

    # required mode trips a cross-validation-skipping pipeline at construction
    required_path = tmp_path / "required.yaml"
    required_path.write_text(
        CONTRACT_PATH.read_text().replace(
            "cross_validation_mode: optional", "cross_validation_mode: required"
        )
    )
    contract_required = load_dataset_contract(required_path, load_schema(SCHEMA_PATH))
    with pytest.raises(StoreContractError, match="cross_validation_mode=required"):
        DailyIngestPipeline(
            tmp_path / "req",
            SCHEMA_PATH,
            required_path,
            SourceRouter(contract_required, {"tdx": FakeMootdxAdapter(mootdx_page())}),
            run_health_probe=False,
        )
    pipeline = DailyIngestPipeline(
        tmp_path / "req2",
        SCHEMA_PATH,
        required_path,
        SourceRouter(contract_required, {"tdx": FakeMootdxAdapter(mootdx_page())}),
        run_health_probe=False,
        require_cross_validation=True,
    )
    assert pipeline.require_cross_validation is True

    # unknown modes are rejected at load
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text(
        CONTRACT_PATH.read_text().replace(
            "cross_validation_mode: optional", "cross_validation_mode: sometimes"
        )
    )
    with pytest.raises(ContractError, match="must be optional or required"):
        load_dataset_contract(bad_path, load_schema(SCHEMA_PATH))


def test_schema_rejects_non_finite_float_values():
    # Residual R9: ±inf passed the float dtype check, so non-finite rows
    # could reach the cross-source gate, where only an equal-infinity pair
    # stays clean (locked by test_gate_equal_infinities_stay_clean). The
    # canonical schema now bans non-finite floats — CanonicalStore publish
    # and read both run Schema.validate — while NaN stays governed by the
    # nullable rule and the gate's U11 behavior is untouched.
    schema = load_schema(SCHEMA_PATH)
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [100.0], "amount": [1050.0],
    })
    schema.validate(frame)
    for bad_value in (np.inf, -np.inf):
        forged = frame.copy()
        forged.loc[0, "close"] = bad_value
        with pytest.raises(ContractError, match="non-finite"):
            schema.validate(forged)
    # NaN still reports through the nullable rule, not the finite ban
    nulled = frame.copy()
    nulled.loc[0, "close"] = np.nan
    with pytest.raises(ContractError, match="non-nullable"):
        schema.validate(nulled)


def test_v3_state_snapshots_list_in_v3_order_without_drops(tmp_path):
    # CR follow-up to the D2 stage-plan single-sourcing (9a91cd2 review):
    # publish_state now numbers V3 states correctly, but
    # list_state_snapshots still ordered by a hardcoded V1 plan and
    # silently dropped any paper_execution snapshot. The listing order is
    # now derived from the states' own contract version.
    from uq.contracts.model_layer import (
        RESEARCH_STAGE_ORDER_V3,
        research_contract_identities,
    )
    from uq.research_chain.resolver import FileResearchRunStore

    store = FileResearchRunStore(tmp_path)
    run_id = "00000000-0000-4000-8000-000000000001"

    def v3_state(stages):
        records = [
            {"stage": stage, "status": "passed", "output_bindings": [], "failure_reason": None}
            for stage in stages
        ]
        state = {
            "contract_version": 3, "schema_version": "1.0.0",
            "request_content_generation_id": "a" * 64,
            "request_manifest_digest_sha256": "b" * 64,
            "run_id": run_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "runner_identity": {
                "code_fingerprint": "c" * 64,
                "environment_profile": "locked-test",
                "lock_digest_sha256": "d" * 64,
            },
            "intent": "execute",
            "stage_records": records,
            "final_status": records[-1]["status"],
            "state_content_generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        state["state_content_generation_id"], state["manifest_digest_sha256"] = (
            research_contract_identities(state, schema_name="research_run_state")
        )
        return state

    prefix = RESEARCH_STAGE_ORDER_V3[: RESEARCH_STAGE_ORDER_V3.index("paper_execution") + 1]
    store.publish_state(v3_state(prefix[:1]), stage="resolve_request")
    store.publish_state(v3_state(prefix), stage="paper_execution")
    snapshots = store.list_state_snapshots("a" * 64, run_id)
    assert [item.stage for item in snapshots] == ["resolve_request", "paper_execution"]
