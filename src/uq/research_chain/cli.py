from __future__ import annotations

import argparse
import json
import math
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..contracts.factor_governance import FactorRegistry
from ..contracts.gate_contracts import validate_contract
from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision
from ..backtest.engine import BacktestEngine, BacktestResultStore
from ..contracts.artifacts import UniverseSnapshotStore
from ..factors.store import FactorStore
from ..errors import ContractError
from ..models.dataset_writer import DatasetWriter
from ..models.predictions import PredictionBuilder
from ..models.trainer import ArtifactStore
from ..portfolio.builder import PortfolioBuilder, TargetWeightStore
from ..portfolio.risk_model import PortfolioRiskReturnsStore
from .provenance import capture_environment, verified_runner_identity
from .telemetry import RunTelemetry
from .owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    BacktestMarketDatasetStore,
    FactorExposureStore,
    FeaturePreprocessingStore,
    FeatureSchemaStore,
    LabelStore,
    SampleConstructionStore,
)
from .adapters import (
    BacktestStageAdapter,
    DatasetStageAdapter,
    FactorStageAdapter,
    ModelStageAdapter,
    PortfolioStageAdapter,
    PredictionStageAdapter,
    QlibExportStageAdapter,
)
from ..contracts.index_membership import IndexMembershipStore
from ..execution.market_dataset_publisher import MarketDatasetPublisher
from ..execution.market_source import PublishedPaperMarketData
from ..execution.risk_source import RiskPolicyOrderSubmissionSource
from ..execution.stage_adapter import PaperExecutionStageAdapter
from ..execution.stores import (
    ExecutionConfigStore,
    ExecutionResultStore,
    OrderPlanStore,
    PaperPortfolioStateStore,
)
from ..risk.stores import RiskDecisionStore
from .contracts import research_request_schema_name
from .resolver import FileResearchRunStore, ResearchChainRequestResolver
from .runner import ResearchChainRunner

_EXIT_CODES = {
    "dry_run_published": 0,
    "published": 0,
    "rejected": 3,
    "configuration_failure": 3,
    "quality_decision_missing": 3,
    "quality_decision_rejected": 3,
    "lineage_mismatch": 3,
    "input_unresolved": 3,
    "input_tampered": 3,
    "request_invalid": 3,
    "store_read_failed": 4,
    "publication_conflict": 5,
    "stage_failed": 4,
}


def uq_research_run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uq-research-run")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("dry-run", "execute"), required=True)
    parser.add_argument("--request-json", type=Path, required=True)
    parser.add_argument("--quality-decisions-json", type=Path)
    parser.add_argument("--scores-json", type=Path)
    parser.add_argument("--decision-provider-json", type=Path)
    parser.add_argument(
        "--membership-root",
        type=Path,
        default=None,
        help="index membership store root; enables the PIT membership-filtered dataset stage",
    )
    parser.add_argument(
        "--paper-risk-documents-json",
        type=Path,
        default=None,
        help="governed risk documents (policy/policy_review/state/state_payload) for paper execution",
    )
    parser.add_argument(
        "--market-dataset-root",
        type=Path,
        default=None,
        help="MarketDatasetPublisher root; supplies the governed trading calendar and paper inputs",
    )
    args = parser.parse_args(argv)

    try:
        args.data_root.mkdir(parents=True, exist_ok=True)
        request = json.loads(args.request_json.read_text(encoding="utf-8"))
        if args.mode == "dry-run":
            result = _dry_run(request, data_root=args.data_root)
        else:
            decisions_document = None
            if args.quality_decisions_json is None:
                raise ContractError("execute mode requires --quality-decisions-json")
            decisions_document = json.loads(args.quality_decisions_json.read_text(encoding="utf-8"))
            # `--scores-json` is optional: without it the prediction stage scores
            # the decision dates by loading the model artifact this same run
            # trains. Requiring the file made real training circular -- the
            # scores only exist after the model does.
            scores_document = (
                json.loads(args.scores_json.read_text(encoding="utf-8"))
                if args.scores_json is not None
                else None
            )
            provider_document = None
            if args.decision_provider_json is not None:
                provider_document = json.loads(args.decision_provider_json.read_text(encoding="utf-8"))
            result = _execute(
                request,
                decisions_document=decisions_document,
                scores_document=scores_document,
                provider_document=provider_document,
                project_root=args.project_root,
                data_root=args.data_root,
                membership_root=args.membership_root,
                market_dataset_root=args.market_dataset_root,
                paper_risk_documents_path=args.paper_risk_documents_json,
            )
    except (ContractError, OSError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        status = getattr(exc, "reason", "configuration_failure")
        if status not in _EXIT_CODES:
            status = "configuration_failure"
        print(json.dumps({"status": status, "errors": [str(exc)]}, sort_keys=True))
        return _EXIT_CODES[status]
    print(json.dumps(result, sort_keys=True))
    return _EXIT_CODES[result["status"]]


def _dry_run(request: dict[str, Any], *, data_root: Path) -> dict[str, Any]:
    # Route by contract version: a v2..v8 request has its own schema name, and
    # hardcoding the v1 schema rejected every later version at the CLI boundary
    # even though the runner's dry-run path is version-aware.
    ModelContractLoader.validate(research_request_schema_name(request), request)
    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    runner.factor_adapter = None
    runner.dataset_adapter = None
    runner.export_adapter = None
    runner.model_adapter = None
    runner.prediction_adapter = None
    runner.portfolio_adapter = None
    runner.backtest_adapter = None
    runner.run_store = FileResearchRunStore(data_root)
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Plan:
        request: dict[str, Any]
        request_manifest_digest_sha256: str

    plan = _Plan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
    )
    environment = request["environment"]
    state = runner.dry_run(
        plan,
        runner_identity={
            "code_fingerprint": environment["code_fingerprint"],
            "environment_profile": "locked",
            "lock_digest_sha256": environment["environment_lock_digest_sha256"],
        },
    )
    return {
        "status": "dry_run_published",
        "request_content_generation_id": request["request_content_generation_id"],
        "run_id": request["run_id"],
        "manifest_path": str(state["manifest_path"]),
        "manifest_digest_sha256": state["manifest_digest_sha256"],
    }


class _MappingDecisionProvider:
    def __init__(self, decisions: Mapping[str, Mapping[str, Any]]) -> None:
        self.decisions = decisions

    def resolve(self, *, output_family: str, subject_generation_id: str, **_: Any) -> Mapping[str, Any]:
        binding_type = _BINDING_TYPE_BY_FAMILY.get(output_family, output_family)
        decision = self.decisions.get(binding_type) or self.decisions.get(output_family)
        if decision is None:
            raise ContractError(f"missing research chain quality decision: {binding_type}")
        # cr.md H6: provider mode stores per-family callables in this mapping;
        # invoke them with the resolved subject instead of dict()-ing the
        # callable (aligned with Runner._decision).
        if callable(decision):
            decision = decision(subject_generation_id)
        if not isinstance(decision, Mapping):
            raise ContractError(f"invalid research chain quality decision: {binding_type}")
        return decision


class _CommandDecisionProvider:
    def __init__(self, *, command: list[str], provider_config_ref: str) -> None:
        self.command = command
        self.provider_config_ref = provider_config_ref

    def resolve(
        self,
        *,
        binding_type: str,
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None,
        output_family: str,
        provider_config_ref: str,
    ) -> Mapping[str, Any]:
        completed = subprocess.run(
            [
                *self.command,
                "--binding-type", binding_type,
                "--subject-generation-id", subject_generation_id,
                "--subject-manifest-digest-sha256", subject_manifest_digest_sha256 or "",
                "--output-family", output_family,
                "--provider-config-ref", provider_config_ref,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "external decision provider failed"
            raise ContractError(detail)
        try:
            decision = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ContractError("external decision provider returned malformed JSON") from exc
        if not isinstance(decision, dict):
            raise ContractError("external decision provider must return an object")
        return decision


class _EnvelopeDecisionProvider:
    """Wrap a raw reviewer decision in the quality-decision envelope.

    The external provider protocol returns the signed reviewer decision, but the
    research resolver and every stage consume the owning-layer envelope
    (``quality_decision.v1``): ``owning_report`` plus the bound subject.  The
    two shapes were being conflated, so provider mode validated raw decisions
    against the envelope schema and failed before any stage ran.
    """

    def __init__(self, provider: Any, *, data_root: Path | None = None) -> None:
        self.provider = provider
        self.provider_config_ref = getattr(provider, "provider_config_ref", "research-cli")
        self.data_root = data_root

    def envelope_for(
        self,
        *,
        binding_type: str,
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None,
    ) -> Mapping[str, Any]:
        """The `quality_decision` envelope the request resolver validates.

        The resolver's schema requires `owning_report` plus the bound subject, so
        a raw reviewer decision cannot be passed to it directly.
        """
        decision = self.provider.resolve(
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_manifest_digest_sha256=subject_manifest_digest_sha256,
            output_family="",
            provider_config_ref=self.provider_config_ref,
        )
        if isinstance(decision, Mapping) and "owning_report" in decision:
            return decision
        if binding_type == "factor_v1":
            raise ContractError(
                "factor quality provider must return the owning report envelope"
            )
        if not isinstance(decision, Mapping):
            raise ContractError("external quality decision must be an object")
        signed_digest = decision.get("subject_content_sha256")
        content_sha256 = signed_digest or subject_manifest_digest_sha256 or subject_generation_id
        expected = _published_quality_checksum(
            binding_type, subject_generation_id, data_root=self.data_root
        )
        if expected is not None:
            stored = self._stored_review(expected)
            if stored is None:
                raise ContractError(
                    "published artifact records a review that is not available in the "
                    f"external review store: {expected}"
                )
            return {
                "contract_version": 1, "schema_version": "1.0.0",
                "binding_type": binding_type,
                "subject_generation_id": subject_generation_id,
                "subject_manifest_digest_sha256": content_sha256,
                "owning_report": stored,
                "decision_checksum_sha256": expected,
                "provider_id": "external-model-quality-reviewer-v1",
                "trust_anchor_id": stored["key_id"],
            }
        report, checksum = bind_reviewed_quality_decision(
            dict(decision),
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_content_sha256=content_sha256,
        )
        return {
            "contract_version": 1, "schema_version": "1.0.0",
            "binding_type": binding_type,
            "subject_generation_id": subject_generation_id,
            "subject_manifest_digest_sha256": content_sha256,
            "owning_report": report,
            "decision_checksum_sha256": checksum,
            "provider_id": "external-model-quality-reviewer-v1",
            "trust_anchor_id": report["key_id"],
        }

    def resolve(
        self,
        *,
        binding_type: str,
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None,
        output_family: str,
        provider_config_ref: str,
    ) -> Mapping[str, Any]:
        """What the *stage flows* consume.

        Two shapes are needed and they are not interchangeable:

        * the request resolver validates a `quality_decision` envelope, so it
          calls `envelope_for`;
        * the stage flows receive the decision unresolved (cr.md U14) and bind
          it themselves, reading `checks` off the *raw* signed decision --
          `PredictionBuilder` requires exactly one `eligibility_coverage` entry,
          which an envelope does not carry at all.

        Where the artifact already records a review checksum, the stored review is
        returned: an external reviewer signs per invocation, so a fresh signature
        has a different checksum and every stage compares against the published
        one.
        """
        expected = _published_quality_checksum(
            binding_type, subject_generation_id, data_root=self.data_root
        )
        if expected is not None:
            stored = self._stored_review(expected)
            if stored is None:
                raise ContractError(
                    "published artifact records a review that is not available in the "
                    f"external review store: {expected}"
                )
            return {
                "contract_version": 1, "schema_version": "1.0.0",
                "binding_type": binding_type,
                "subject_generation_id": subject_generation_id,
                "subject_manifest_digest_sha256": (
                    subject_manifest_digest_sha256 or subject_generation_id
                ),
                "owning_report": stored,
                "decision_checksum_sha256": expected,
                "provider_id": "external-model-quality-reviewer-v1",
                "trust_anchor_id": stored["key_id"],
            }
        decision = self.provider.resolve(
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_manifest_digest_sha256=subject_manifest_digest_sha256,
            output_family=output_family,
            provider_config_ref=provider_config_ref,
        )
        if binding_type == "factor_v1":
            # Factor reviews are already owning reports in the factor store's
            # `quality_report.v1` shape; the provider returns that report
            # directly and it must not be wrapped again.
            if isinstance(decision, Mapping) and "owning_report" in decision:
                return decision
            raise ContractError(
                "factor quality provider must return the owning report envelope"
            )
        if not isinstance(decision, Mapping):
            raise ContractError("external quality decision must be an object")
        return dict(decision)

    def _stored_review(self, checksum: str) -> Mapping[str, Any] | None:
        if self.data_root is None:
            return None
        # The review store root follows the owning store, not one fixed directory.
        # `FeaturePreprocessingStore` and `BacktestConfigStore` are constructed
        # under `<data-root>/governed` by `_build_runner`, and their publishers
        # write their review copies beside that store.  Looking only at
        # `<data-root>/external_quality_reviews` made a correctly published
        # preprocessing artifact unresolvable at execute time: the manifest named
        # its review checksum, but the only copy was under `governed/`.
        for root in (self.data_root, self.data_root / "governed"):
            path = root / "external_quality_reviews" / f"{checksum}.json"
            if not path.is_file():
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(document, Mapping):
                return document
        return None


def _published_quality_checksum(
    binding_type: str, subject_generation_id: str, *, data_root: Path | None
) -> str | None:
    """The review checksum an already-published artifact records, if readable.

    The external provider is re-invoked at execute time, so its signature is
    fresh; the artifact, however, was published with one specific review.  A
    mismatch is a governance failure (the review does not describe the bytes on
    disk), not a reason to re-sign.  Unknown families return ``None`` and are
    left to the owning stage's own verification.
    """
    from .owning_contracts import (
        BacktestConfigStore,
        FeaturePreprocessingStore,
        LabelStore,
        SampleConstructionStore,
    )

    if data_root is None:
        return None
    try:
        if binding_type == "label_set_v1":
            # `label_set.v1` is an owning document with no quality field; its
            # `data_checksum_sha256` is the label-row checksum, not a review
            # checksum.  Returning it here made every valid label set look as
            # though it had a review that could not be found.  The label stage
            # verifies its decision itself, so there is nothing to recover.
            return None
        if binding_type == "feature_preprocessing_v1":
            return FeaturePreprocessingStore(data_root / "governed").read_manifest(
                subject_generation_id
            )["quality_report_checksum_sha256"]
        if binding_type == "feature_preprocessing_v2":
            return FeaturePreprocessingStore(data_root / "governed").read_manifest(
                subject_generation_id
            )["quality_report_checksum_sha256"]
        if binding_type == "sample_construction_v1":
            return SampleConstructionStore(data_root / "governed").read_manifest(
                subject_generation_id
            )["quality_report_checksum_sha256"]
        if binding_type == "backtest_config_v1":
            # The config's review subject is its *provisional* generation, not
            # the final generation id the request binds.  The resolver only has
            # the latter, so it cannot look the review up by that key; the
            # backtest stage re-derives the provisional subject and verifies the
            # recorded checksum itself.
            return None
    except (ContractError, KeyError, TypeError):
        return None
    return None


def _envelope_decision_for_family(provider: Any, output_family: str):
    """The resolver-facing per-family decision callable.

    `ResearchChainRequestResolver` validates each binding against the
    `quality_decision` envelope schema, which needs `owning_report`; the raw
    reviewer decision does not satisfy it.  This adapter hands the provider the
    binding type so it can build (or recover) the envelope.
    """
    binding_type = _BINDING_TYPE_BY_FAMILY[output_family]

    def resolve(
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None = None,
    ) -> Mapping[str, Any]:
        return provider.envelope_for(
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_manifest_digest_sha256=subject_manifest_digest_sha256,
        )

    return resolve


def _decision_provider_for_family(provider: Any, output_family: str):
    binding_type = _BINDING_TYPE_BY_FAMILY[output_family]

    # cr.md U14: two-argument so flows whose subject content digest differs
    # from the generation id (qlib export/receipt, target_weights) forward the
    # subject digest to the external provider; one-argument legacy callables
    # default the digest to the generation id, so they fail closed only at
    # those genuinely content-differing flows (elsewhere content==generation).
    def resolve(
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None = None,
    ) -> Mapping[str, Any]:
        return provider.resolve(
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_manifest_digest_sha256=subject_manifest_digest_sha256,
            output_family=output_family,
            provider_config_ref=provider.provider_config_ref,
        )

    return resolve


_BINDING_TYPE_BY_FAMILY = {
    "factor_partition": "factor_v1",
    "label_set": "label_set_v1",
    "feature_preprocessing": "feature_preprocessing_v1",
    "feature_preprocessing_v2": "feature_preprocessing_v2",
    "sample_construction": "sample_construction_v1",
    "model_dataset": "model_dataset_v1",
    "qlib_dataset_export": "qlib_dataset_export_v1",
    "qlib_init_receipt": "qlib_init_receipt_v1",
    "model_run": "model_run_v1",
    "model_artifact": "model_artifact_v1",
    "prediction_set": "prediction_set_v1",
    "portfolio_definition": "portfolio_definition_v1",
    "target_weights": "target_weights_v1",
    "backtest_config": "backtest_config_v1",
    "backtest_result": "backtest_result_v1",
    "execution_config": "execution_config_v1",
    "order_plan": "order_plan_v1",
    "execution_result": "execution_result_v1",
    "paper_portfolio_state": "paper_portfolio_state_v1",
    "research_run_result": "research_run_result_v1",
}


def _load_scores(document: Any) -> dict[str, pd.DataFrame]:
    validate_contract("research_scores.v1.json", document)
    scores: dict[str, pd.DataFrame] = {}
    for decision_date, rows in document["scores_by_decision_date"].items():
        frame = pd.DataFrame(rows)
        frame["score"] = pd.to_numeric(frame["score"], errors="raise")
        if not frame["score"].map(math.isfinite).all():
            raise ContractError(f"non-finite scores for decision date {decision_date}")
        if frame["instrument"].map(lambda value: not isinstance(value, str) or not value.strip()).any():
            raise ContractError(f"invalid instruments for decision date {decision_date}")
        scores[decision_date] = frame[["instrument", "score"]]
    return scores


def _governed_trading_days(publisher: MarketDatasetPublisher, request: Mapping[str, Any]) -> list[date]:
    """Derive the research window sessions from the published governed calendar.

    The calendar semantics are single-sourced in ``PublishedPaperMarketData``:
    partitions are selected by partition date inside the window (the
    set-binding convention) and must jointly cover the whole window, so a
    partially covered window fails closed instead of silently narrowing the
    observed session set.
    """
    return PublishedPaperMarketData(
        publisher,
        window_start=date.fromisoformat(request["window_start_date"]),
        window_end=date.fromisoformat(request["window_end_date"]),
    ).sessions()


def _build_runner(
    *,
    project_root: Path,
    data_root: Path,
    run_store: FileResearchRunStore,
    membership_store: IndexMembershipStore | None = None,
    trading_calendar: Sequence[date] | None = None,
    paper_adapter: Any | None = None,
):
    factor_store = FactorStore(data_root, FactorRegistry(project_root))
    feature_store_root = data_root / "governed"
    universe_store = UniverseSnapshotStore(data_root)
    dataset_adapter = DatasetStageAdapter(
        factor_store=factor_store,
        adjusted_price_store=AdjustedPriceDatasetStore(data_root),
        label_store=LabelStore(data_root),
        universe_store=universe_store,
        feature_schema_store=FeatureSchemaStore(feature_store_root),
        preprocessing_store=FeaturePreprocessingStore(feature_store_root),
        factor_exposure_store=FactorExposureStore(feature_store_root),
        sample_construction_store=SampleConstructionStore(feature_store_root),
        dataset_writer=DatasetWriter(data_root),
        run_store=run_store,
        membership_store=membership_store,
        trading_calendar=trading_calendar,
    )
    from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder
    from uq.models.qlib_runtime import QlibRuntimeTrainer

    exporter = QlibDatasetExporter(data_root / "qlib_exports")
    dataset_writer = DatasetWriter(data_root)
    prediction_builder = PredictionBuilder(data_root)
    portfolio_builder = PortfolioBuilder(data_root)
    risk_returns_store = PortfolioRiskReturnsStore(
        data_root, calendar_store=BacktestMarketDatasetStore(data_root)
    )
    return ResearchChainRunner(
        factor_adapter=FactorStageAdapter(factor_store, run_store),
        dataset_adapter=dataset_adapter,
        export_adapter=QlibExportStageAdapter(
            exporter=exporter,
            receipt_builder=QlibInitReceiptBuilder(),
            dataset_writer=dataset_writer,
            run_store=run_store,
        ),
        model_adapter=ModelStageAdapter(
            trainer=QlibRuntimeTrainer(),
            exporter=exporter,
            artifact_store=ArtifactStore(data_root),
            dataset_writer=dataset_writer,
            universe_store=universe_store,
            run_store=run_store,
            factor_store=factor_store,
        ),
        prediction_adapter=PredictionStageAdapter(prediction_builder, run_store),
        portfolio_adapter=PortfolioStageAdapter(
            universe_store=universe_store,
            portfolio_builder=portfolio_builder,
            target_weight_store=TargetWeightStore(data_root),
            run_store=run_store,
            prediction_builder=prediction_builder,
            # specs/layers/portfolio-backtest-layer-spec.md 6.1: the
            # corporate-action exclusion happens at universe filtering time, so
            # the portfolio stage needs the same two stores the backtest stage
            # reads its exclusion set from.
            backtest_config_store=BacktestConfigStore(data_root),
            market_dataset_store=BacktestMarketDatasetStore(data_root),
            risk_returns_store=risk_returns_store,
        ),
        backtest_adapter=BacktestStageAdapter(
            backtest_engine=BacktestEngine(data_root),
            backtest_result_store=BacktestResultStore(data_root),
            backtest_config_store=BacktestConfigStore(data_root),
            run_store=run_store,
            adjusted_price_store=dataset_adapter.adjusted_price_store,
            market_dataset_store=BacktestMarketDatasetStore(data_root),
        ),
        run_store=run_store,
        paper_adapter=paper_adapter,
    )


def _execute(
    request: dict[str, Any],
    *,
    decisions_document: dict[str, Any],
    scores_document: dict[str, Any],
    project_root: Path,
    data_root: Path,
    provider_document: dict[str, Any] | None = None,
    membership_root: Path | None = None,
    market_dataset_root: Path | None = None,
    paper_risk_documents_path: Path | None = None,
) -> dict[str, Any]:
    # `--decision-provider-json` is the production shape: every family is
    # resolved by an external reviewer for the subject the stage derives, so a
    # static `quality_decisions` mapping is neither required nor used.  The
    # entry point used to demand one unconditionally and then overwrite it with
    # the provider mapping, which made provider-only invocation impossible even
    # though the resolver and runner support it.
    if provider_document is None:
        if not isinstance(decisions_document.get("quality_decisions"), dict):
            raise ContractError("quality decisions document requires quality_decisions")
        decisions = decisions_document["quality_decisions"]
        # Static documents can carry the resolver's owning-report envelopes
        # separately from the raw decisions consumed by stage implementations.
        envelope_decisions = decisions_document.get("request_quality_decisions", decisions)
        if not isinstance(envelope_decisions, dict):
            raise ContractError("request_quality_decisions must be an object")
    else:
        decisions = {}
    if provider_document is not None:
        command = provider_document.get("provider_command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ContractError("decision provider document requires provider_command")
        if provider_document.get("provider_id") != "external-model-quality-reviewer-v1":
            raise ContractError("unsupported decision provider identity")
        command_provider = _CommandDecisionProvider(
            command=command,
            provider_config_ref=provider_document.get("provider_config_ref", "research-cli"),
        )
        # The external reviewer returns the raw signed decision.  Its two
        # consumers need two different shapes, and conflating them is what made
        # provider mode fail before any stage ran:
        #   * the request resolver validates the `quality_decision` envelope
        #     (`owning_report` plus the bound subject), so it needs the wrapped
        #     form;
        #   * the stage flows receive the decision unresolved (cr.md U14) and
        #     bind it themselves, reading `checks` off the *raw* decision --
        #     `PredictionBuilder` requires exactly one `eligibility_coverage`
        #     entry, which an envelope does not carry at all.
        # The runner's decisions must resolve to the review the artifact was
        # *published* with, not a fresh signature.  An external reviewer signs
        # per invocation, so its checksum changes every call; the owning store
        # records the one it was published with and every stage compares against
        # that.  `_EnvelopeDecisionProvider` performs that recovery for the
        # families that record a review checksum, and passes the raw decision
        # through for the stage-owned families that bind it themselves.
        decisions = {
            family: _decision_provider_for_family(
                _EnvelopeDecisionProvider(command_provider, data_root=data_root), family
            )
            for family in _BINDING_TYPE_BY_FAMILY
        }
        envelope_provider = _EnvelopeDecisionProvider(command_provider, data_root=data_root)
        envelope_decisions = {
            family: _envelope_decision_for_family(envelope_provider, family)
            for family in _BINDING_TYPE_BY_FAMILY
        }
    scores = _load_scores(scores_document) if scores_document is not None else None
    run_store = FileResearchRunStore(data_root)
    membership_store = IndexMembershipStore(membership_root) if membership_root is not None else None
    market_publisher = (
        MarketDatasetPublisher(market_dataset_root) if market_dataset_root is not None else None
    )
    if membership_store is not None and market_publisher is not None:
        trading_calendar = _governed_trading_days(market_publisher, request)
    elif membership_store is not None or market_publisher is not None:
        raise ContractError(
            "--membership-root and --market-dataset-root must be provided together"
        )
    else:
        trading_calendar = None
    paper_adapter = None
    paper_risk_source = None
    if request.get("contract_version") in {5, 6, 7, 8}:
        if market_publisher is None:
            raise ContractError(
                f"research request v{request.get('contract_version')} requires --market-dataset-root"
            )
        if paper_risk_documents_path is None:
            raise ContractError(
                f"research request v{request.get('contract_version')} requires --paper-risk-documents-json"
            )
        paper_root = data_root / "paper-execution"
        market = PublishedPaperMarketData(
            market_publisher,
            window_start=date.fromisoformat(request["window_start_date"]),
            window_end=date.fromisoformat(request["window_end_date"]),
        )
        paper_adapter = PaperExecutionStageAdapter(
            request["paper_execution_template"],
            market=market,
            config_store=ExecutionConfigStore(paper_root),
            plan_store=OrderPlanStore(paper_root),
            result_store=ExecutionResultStore(paper_root),
            state_store=PaperPortfolioStateStore(paper_root),
            risk_decision_store=RiskDecisionStore(paper_root / "risk"),
        )
        documents = json.loads(paper_risk_documents_path.read_text(encoding="utf-8"))
        paper_risk_source = RiskPolicyOrderSubmissionSource(
            policy=documents["policy"],
            policy_review=documents["policy_review"],
            state=documents.get("state"),
            state_payload=documents.get("state_payload"),
        )
    runner = _build_runner(
        project_root=project_root,
        data_root=data_root,
        run_store=run_store,
        membership_store=membership_store,
        trading_calendar=trading_calendar,
        paper_adapter=paper_adapter,
    )
    resolver = ResearchChainRequestResolver(
        {
            "factor_partition": runner.factor_adapter.store,
            "universe_snapshot": runner.dataset_adapter.universe_store,
            "adjusted_price_dataset": runner.dataset_adapter.adjusted_price_store,
            "label_set": runner.dataset_adapter.label_store,
            "feature_preprocessing": runner.dataset_adapter.preprocessing_store,
            "factor_exposure_panel": getattr(
                runner.dataset_adapter, "factor_exposure_store", None
            ),
            "sample_construction": getattr(
                runner.dataset_adapter, "sample_construction_store", None
            ),
            "backtest_config": runner.backtest_adapter.backtest_config_store,
            "portfolio_risk_returns": PortfolioRiskReturnsStore(
                data_root, calendar_store=BacktestMarketDatasetStore(data_root)
            ),
        },
        membership_store=membership_store,
        trading_calendar=trading_calendar,
        market_dataset_store=market_publisher,
    )
    try:
        plan = resolver.resolve(
            request,
            quality_provider=_MappingDecisionProvider(envelope_decisions),
            provider_config_ref="research-cli",
        )
    except Exception as exc:
        status = getattr(exc, "reason", None)
        if status not in {"request_invalid", "input_unresolved", "input_tampered", "lineage_mismatch", "quality_decision_missing", "quality_decision_rejected", "store_read_failed"}:
            status = "configuration_failure"
        error = ContractError(status, str(exc))
        error.reason = status
        raise error from exc
    if project_root.resolve() != Path(__file__).resolve().parents[3]:
        raise ContractError("--project-root does not match the executing uq checkout")
    environment = capture_environment(project_root)
    runner_identity = verified_runner_identity(request["environment"], environment)
    with RunTelemetry(data_root, request, environment) as telemetry:
        outcome = runner.execute(
            plan,
            runner_identity=runner_identity,
            telemetry=telemetry,
            quality_decisions=decisions,
            scores_by_decision_date=scores,
            # Without an external scores file the runner decides which dates to score
            # (it defaults to the window end); with one, the file's dates are
            # authoritative -- that is the option-4 contract from cr.md 8.75.
            decision_dates=sorted(scores) if scores is not None else None,
            paper_risk_decision_source=paper_risk_source,
        )
    return {
        "status": "published",
        "request_content_generation_id": request["request_content_generation_id"],
        "run_id": request["run_id"],
        "result_content_generation_id": outcome.result_manifest["result_content_generation_id"],
        "manifest_path": str(outcome.result_path),
        "manifest_digest_sha256": outcome.result_manifest["manifest_digest_sha256"],
        "final_status": outcome.result_manifest["final_status"],
        "telemetry_path": str(telemetry.path),
    }


if __name__ == "__main__":
    raise SystemExit(uq_research_run())
