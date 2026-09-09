from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.model_layer import paper_execution_identities, review_signature
from uq.execution.stores import read_verified_quality_report
from uq.errors import ContractError
from uq.execution.planner import PaperOrderPlanner
from uq.execution.stores import (
    ExecutionConfigStore,
    ExecutionResultStore,
    OrderPlanStore,
    PaperPortfolioStateStore,
    _validate_bound_quality_report,
)
from uq.risk.contracts import risk_contract_identities
from uq.risk.publication import ConfigPublicationBinding
from tests.review_key import REVIEWER_PRIVATE_KEY

from tests.test_paper_execution_phase2 import (
    _config,
    _decision_for_review,
    _decision_prices,
    _execute,
    _execution_market,
    _publish_config,
    _publish_plan,
    _QUALITY_CHECKS_BY_FAMILY,
    _recompute,
    _target,
    _unsigned,
)
from tests.test_paper_execution_phase3 import (
    _publish_initial_state,
    _STATE_QUALITY_CHECKS,
)

RISK_FIXTURE = Path("config/schemas/fixtures/risk/risk_decision-valid.json")


def _risk(config: dict, action: str = "allow") -> dict:
    risk = json.loads(RISK_FIXTURE.read_text())
    risk["decision_scope"] = "order_submission"
    risk["action"] = action
    risk["as_of_date"] = config["decision_date"]
    risk["visible_through"] = f"{config['execution_date']}T15:00:00+08:00"
    for _ in range(8):
        risk["binding_config_generation_id"] = config["generation_id"]
        risk["generation_id"], risk["manifest_digest_sha256"] = risk_contract_identities(
            risk, schema_name="risk_decision"
        )
        binding = {
            "family": "risk_decision_v1",
            "generation_id": risk["generation_id"],
            "manifest_digest_sha256": risk["manifest_digest_sha256"],
        }
        if config.get("risk_decision_binding") == binding:
            return risk
        config["risk_decision_binding"] = binding
        config = _recompute(config)
    raise AssertionError("risk decision did not converge")


def _publish_action_config(tmp_path: Path, action: str) -> tuple[dict, dict]:
    if action not in {"allow", "warn"}:
        raise ValueError("action must be executable")
    store = ExecutionConfigStore(tmp_path)
    config = _config()
    risk = _risk(config, action)
    generation = config["generation_id"]
    unsigned = _unsigned("execution_config_v1", generation)
    quality = _decision_for_review(unsigned, "execution_config_v1", generation)
    store.publish(
        config,
        quality_decision=quality,
        risk_decision=risk,
        config_publication=ConfigPublicationBinding(
            decision=risk, config_generation_id=generation
        ),
    )
    return store.read(generation), risk


def _blocked_config_risk(action: str) -> tuple[dict, dict]:
    config = _config()
    risk = json.loads(RISK_FIXTURE.read_text())
    risk["decision_scope"] = "order_submission"
    risk["action"] = action
    risk["binding_config_generation_id"] = config["generation_id"]
    risk["generation_id"], risk["manifest_digest_sha256"] = risk_contract_identities(
        risk, schema_name="risk_decision"
    )
    config["risk_decision_binding"] = {
        "family": "risk_decision_v1",
        "generation_id": risk["generation_id"],
        "manifest_digest_sha256": risk["manifest_digest_sha256"],
    }
    config = _recompute(config)
    return config, risk


def _publish_action_plan(tmp_path: Path, action: str) -> tuple[dict, pd.DataFrame]:
    if action not in {"allow", "warn"}:
        raise ValueError("action must be executable")
    config, risk = _publish_action_config(tmp_path, action)
    target = _target()
    frame, residual = PaperOrderPlanner().plan(
        config, target, None, _decision_prices(), opening_cash=config["initial_state"]["cash"]
    )
    manifest = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "execution_id": config["execution_id"],
        "state_mode": config["state_mode"],
        "execution_config_generation_id": config["generation_id"],
        "execution_date": config["execution_date"],
        "decision_date": config["decision_date"],
        "data_file": "data.parquet",
        "data_checksum_sha256": "0" * 64,
        "columns": PaperOrderPlanner.COLUMNS,
        "dtypes": PaperOrderPlanner.DTYPES,
        "row_count": int(len(frame)),
        "key_uniqueness": ["plan_sequence"],
        "logical_fingerprint": "3" * 64,
        "serialization_profile_id": "parquet-v1",
        "target_weights_binding": config["target_weights_binding"],
        "input_state_binding": config["input_state_binding"],
        "initial_state_provenance_sha256": config["initial_state"]["provenance_sha256"],
        "market_data_binding": {
            "family": config["market_data_binding"]["dataset_family"],
            "generation_id": config["market_data_binding"]["generation_id"],
            "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
        },
        "calendar_binding": config["calendar_binding"],
        "suspension_binding": config["suspension_binding"],
        "corporate_action_binding": config["corporate_action_binding"],
        "risk_decision_binding": config["risk_decision_binding"],
        "ordered_cash_residual_sequence": residual,
        "run_id": config["run_id"],
        "created_at": config["created_at"],
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "generation_id": "0" * 64,
    }
    generation = config["generation_id"]
    unsigned = _unsigned("order_plan_v1", generation)
    quality = _decision_for_review(unsigned, "order_plan_v1", generation)
    store = OrderPlanStore(tmp_path)
    store.publish(manifest, frame, quality_decision=quality, risk_decision=risk)
    published, published_frame = store.read(manifest["generation_id"])
    return published, published_frame


def test_publisher_generated_passed_report_cannot_enable_publication(tmp_path: Path) -> None:
    root = tmp_path / "publisher"
    root.mkdir()
    config, risk = _publish_action_config(root, "allow")
    tampered_manifest = copy.deepcopy(config)
    tampered_manifest["row_count"] = 0
    tampered_manifest["generation_id"], tampered_manifest["manifest_digest_sha256"] = (
        paper_execution_identities(tampered_manifest, schema_name="execution_config")
    )
    with pytest.raises(ContractError, match="not bound to this execution config generation"):
        ExecutionConfigStore(root).publish(
            tampered_manifest,
            quality_decision=_decision_for_review(
                _unsigned("execution_config_v1", tampered_manifest["generation_id"]),
                "execution_config_v1",
                tampered_manifest["generation_id"],
            ),
            risk_decision=risk,
            config_publication=ConfigPublicationBinding(
                decision=risk,
                config_generation_id=tampered_manifest["generation_id"],
            ),
        )


def test_reviewed_report_exact_checks_required(tmp_path: Path) -> None:
    root = tmp_path / "exact"
    root.mkdir()
    config, risk = _publish_action_config(root, "allow")
    config["decision_date"] = "2026-01-06"
    config["execution_date"] = "2026-01-07"
    config["risk_decision_binding"] = None
    config = _recompute(config)
    risk = _risk(config, "allow")
    unsigned = _unsigned("execution_config_v1", config["generation_id"])
    unsigned["checks"] = unsigned["checks"][:-1]
    decision = _decision_for_review(unsigned, "execution_config_v1", config["generation_id"])
    store = ExecutionConfigStore(tmp_path)
    with pytest.raises(ContractError, match="checks do not exactly match"):
        store.publish(
            config,
            quality_decision=decision,
            risk_decision=risk,
            config_publication=ConfigPublicationBinding(
                decision=risk, config_generation_id=config["generation_id"]
            ),
        )


@pytest.mark.parametrize("action", ["resize", "block", "block_order", "block_new_buy", "de_risk", "flatten", "halt_strategy", "escalate_review"])


def test_all_blocking_risk_actions_block_publication_chain(tmp_path: Path, action: str) -> None:
    config, risk = _blocked_config_risk(action)
    unsigned = _unsigned("execution_config_v1", config["generation_id"])
    quality = _decision_for_review(unsigned, "execution_config_v1", config["generation_id"])
    with pytest.raises(ContractError, match="not executable"):
        ExecutionConfigStore(tmp_path).publish(
            config,
            quality_decision=quality,
            risk_decision=risk,
            config_publication=ConfigPublicationBinding(
                decision=risk,
                config_generation_id=config["generation_id"],
                require_executable=False,
            ),
        )
    assert list(tmp_path.rglob("manifest.json")) == []


def test_allow_and_warn_are_executable_noop(tmp_path: Path) -> None:
    for action in ("allow", "warn"):
        root = tmp_path / action
        root.mkdir()
        config, risk = _publish_action_config(root, action)
        target = _target()
        frame, residual = PaperOrderPlanner().plan(
            config, target, None, _decision_prices(), opening_cash=config["initial_state"]["cash"]
        )
        manifest = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "execution_id": config["execution_id"],
            "state_mode": config["state_mode"],
            "execution_config_generation_id": config["generation_id"],
            "execution_date": config["execution_date"],
            "decision_date": config["decision_date"],
            "data_file": "data.parquet",
            "data_checksum_sha256": "0" * 64,
            "columns": PaperOrderPlanner.COLUMNS,
            "dtypes": PaperOrderPlanner.DTYPES,
            "row_count": int(len(frame)),
            "key_uniqueness": ["plan_sequence"],
            "logical_fingerprint": "3" * 64,
            "serialization_profile_id": "parquet-v1",
            "target_weights_binding": config["target_weights_binding"],
            "input_state_binding": config["input_state_binding"],
            "initial_state_provenance_sha256": config["initial_state"]["provenance_sha256"],
            "market_data_binding": {
                "family": config["market_data_binding"]["dataset_family"],
                "generation_id": config["market_data_binding"]["generation_id"],
                "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
            },
            "calendar_binding": config["calendar_binding"],
            "suspension_binding": config["suspension_binding"],
            "corporate_action_binding": config["corporate_action_binding"],
            "risk_decision_binding": config["risk_decision_binding"],
            "ordered_cash_residual_sequence": residual,
            "run_id": config["run_id"],
            "created_at": config["created_at"],
            "quality_report_checksum_sha256": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
            "generation_id": "0" * 64,
        }
        unsigned = _unsigned("order_plan_v1", config["generation_id"])
        quality = _decision_for_review(unsigned, "order_plan_v1", config["generation_id"])
        store = OrderPlanStore(root)
        store.publish(manifest, frame, quality_decision=quality, risk_decision=risk)
        assert store.read(manifest["generation_id"])[0]["generation_id"] == manifest["generation_id"]


def test_missing_report_readback_fails(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    root.mkdir()
    config, partition = _publish_config(root)
    report_path = ExecutionConfigStore(root).root / "external_quality_reviews" / f"{config['quality_report_checksum_sha256']}.json"
    report_path.unlink()
    with pytest.raises(ContractError, match="missing"):
        ExecutionConfigStore(root).read(config["generation_id"])


def test_tampered_report_readback_fails(tmp_path: Path) -> None:
    config, partition = _publish_config(tmp_path)
    report_path = ExecutionConfigStore(tmp_path).root / "external_quality_reviews" / f"{config['quality_report_checksum_sha256']}.json"
    report = json.loads(report_path.read_text())
    report["status"] = "warning"
    report_path.write_text(json.dumps(report, sort_keys=True) + "\n")
    with pytest.raises(ContractError, match="canonical checksum"):
        ExecutionConfigStore(tmp_path).read(config["generation_id"])


def test_forged_report_readback_fails(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    root = tmp_path / "forged"
    root.mkdir()
    config, _ = _publish_config(root)
    report_path = ExecutionConfigStore(root).root / "external_quality_reviews" / f"{config['quality_report_checksum_sha256']}.json"
    report = json.loads(report_path.read_text())
    unsigned = {
        key: value for key, value in report.items()
        if key not in {"review_signature_sha256", "report_checksum_sha256", "bound_generation_id", "subject_content_sha256"}
    }
    wrong_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("11" * 32))
    key_path = Path(tempfile.gettempdir()) / "uq-wrong-reviewer.pem"
    key_path.write_bytes(wrong_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    report["review_signature_sha256"] = review_signature(unsigned, private_key_pem=key_path)
    from uq.contracts.model_layer import sha256_json
    checksum = sha256_json(report)
    forged_path = report_path.parent / f"{checksum}.json"
    forged_path.write_text(json.dumps(report, sort_keys=True) + "\n")
    manifest = {"quality_report_checksum_sha256": checksum}
    with pytest.raises(ContractError, match="canonical checksum mismatch"):
        read_verified_quality_report(root, manifest)


def test_expired_risk_decision_blocks_publication(tmp_path: Path) -> None:
    config = _config()
    risk = _risk(config)
    risk["visible_through"] = "2020-01-01T15:00:00+08:00"
    risk["generation_id"], risk["manifest_digest_sha256"] = risk_contract_identities(
        risk, schema_name="risk_decision"
    )
    config["risk_decision_binding"] = {
        "family": "risk_decision_v1",
        "generation_id": risk["generation_id"],
        "manifest_digest_sha256": risk["manifest_digest_sha256"],
    }
    config = _recompute(config)
    unsigned = _unsigned("execution_config_v1", config["generation_id"])
    quality = _decision_for_review(unsigned, "execution_config_v1", config["generation_id"])
    with pytest.raises(ContractError, match="expired for paper execution"):
        ExecutionConfigStore(tmp_path).publish(
            config,
            quality_decision=quality,
            risk_decision=risk,
            config_publication=ConfigPublicationBinding(
                decision=risk,
                config_generation_id=config["generation_id"],
                require_executable=False,
            ),
        )


def test_mismatched_report_generation_blocks_publication(tmp_path: Path) -> None:
    from uq.contracts.model_layer import bind_reviewed_quality_decision, sha256_json

    root = tmp_path / "mismatch"
    root.mkdir()
    config, _risk = _publish_action_config(root, "allow")
    decision = _decision_for_review(
        _unsigned("order_plan_v1", config["generation_id"]),
        "order_plan_v1",
        config["generation_id"],
    )
    report, _ = bind_reviewed_quality_decision(
        decision,
        binding_type="order_plan_v1",
        subject_generation_id=config["generation_id"],
    )
    report["bound_generation_id"] = "e" * 64
    report["subject_content_sha256"] = report["bound_generation_id"]
    checksum = sha256_json({
        key: value for key, value in report.items()
        if key != "report_checksum_sha256"
    })
    report["report_checksum_sha256"] = checksum
    report_path = root / "external_quality_reviews" / f"{checksum}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, sort_keys=True) + "\n")
    with pytest.raises(ContractError, match="another generation"):
        _validate_bound_quality_report(
            root,
            {
                "generation_id": config["generation_id"],
                "quality_report_checksum_sha256": checksum,
            },
            "order_plan_v1",
        )


def test_production_rejects_test_key_anchor(monkeypatch) -> None:
    from uq.runtime import require_production_review_key

    monkeypatch.setenv("UQ_RUNTIME_MODE", "production")
    with pytest.raises(ContractError, match="test-mode Ed25519 trust anchor"):
        require_production_review_key(
            "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29",
            context="paper execution",
        )


def test_paper_runtime_cannot_enter_broker_path() -> None:
    from uq.runtime import current_runtime_mode

    assert current_runtime_mode() != "broker"
    assert not Path("src/uq/execution/broker.py").exists()
    assert "broker" not in Path("src/uq/runtime.py").read_text().split("mode not in {", 1)[1].split("}", 1)[0]


def test_research_prod_trust_anchor_unchanged(monkeypatch) -> None:
    from uq.runtime import require_production_review_key

    key = "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"
    monkeypatch.setenv("UQ_RUNTIME_MODE", "research")
    require_production_review_key(key, context="paper execution")
    monkeypatch.setenv("UQ_RUNTIME_MODE", "production")
    with pytest.raises(ContractError, match="test-mode Ed25519 trust anchor"):
        require_production_review_key(key, context="paper execution")
