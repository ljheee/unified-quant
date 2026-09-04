from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID
from typing import Any, Mapping, Protocol

from ..errors import ContractError


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    trust_anchor_id: str
    config_path: str


class QualityDecisionProvider(Protocol):
    def resolve(
        self,
        *,
        binding_type: str,
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None,
        output_family: str,
        provider_config_ref: str,
    ) -> Mapping[str, Any]:
        """Return an immutable owning-layer decision; implementations never sign."""


@dataclass(frozen=True)
class PublishedRequest:
    manifest_path: Path
    manifest_digest_sha256: str


@dataclass(frozen=True)
class StateSummary:
    stage: str
    manifest_digest_sha256: str
    status: str


@dataclass(frozen=True)
class PublishedState:
    manifest_path: Path
    manifest_digest_sha256: str


@dataclass(frozen=True)
class PublishedResult:
    manifest_path: Path
    manifest_digest_sha256: str


class ResearchRunStore(Protocol):
    def publish_request(self, manifest: Mapping[str, Any], *, path_policy: str) -> PublishedRequest:
        """Atomically publish the attempt-local request manifest."""

    def read_request(self, request_content_generation_id: str, manifest_digest_sha256: str) -> Mapping[str, Any]:
        """Manifest-first readback; reject digest, path, and identity mismatches."""

    def publish_state(self, manifest: Mapping[str, Any], *, stage: str) -> PublishedState:
        """Atomically publish an append-forward state snapshot."""

    def read_state(
        self,
        request_content_generation_id: str,
        run_id: str,
        stage: str,
        manifest_digest_sha256: str,
    ) -> Mapping[str, Any]:
        """Read a verified state snapshot without accepting it as a downstream input."""

    def list_state_snapshots(
        self, request_content_generation_id: str, run_id: str
    ) -> list[StateSummary]:
        """List complete snapshots in normative stage order."""

    def publish_result(self, manifest: Mapping[str, Any], *, path_policy: str) -> PublishedResult:
        """Atomically publish a successful result; implemented in Phase 5."""

    def read_result(self, result_generation_id: str, manifest_digest_sha256: str) -> Mapping[str, Any]:
        """Read a verified successful result and its evidence bindings."""



_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_layout_common(candidate: Path) -> Path:
    if candidate.is_absolute():
        raise ContractError("research path must be relative")
    if "\\" in str(candidate):
        raise ContractError("research path must not contain backslashes")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ContractError("research path must not traverse")
    if any(part.startswith(".") for part in candidate.parts):
        raise ContractError("research path must not contain hidden segments")
    return candidate


def validate_research_layout(
    path: Path | str,
    *,
    data_root: Path,
    kind: str,
    request_generation_id: str,
    run_id: str,
    stage: str | None = None,
    result_generation_id: str | None = None,
) -> Path:
    """Validate the frozen physical path without creating or accepting it."""
    stage_order = [
        "resolve_request", "factor_computation", "dataset_preparation", "qlib_export",
        "model_training", "prediction_publication", "portfolio_construction",
        "backtest_execution", "result_reconciliation",
    ]
    if not isinstance(request_generation_id, str) or not _SHA256_PATTERN.fullmatch(request_generation_id):
        raise ContractError("invalid request generation in research path")
    try:
        UUID(run_id, version=4)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError("invalid run id in research path") from exc
    if kind == "request":
        expected = Path("research_runs/requests") / f"request={request_generation_id}" / f"run={run_id}" / "manifest.json"
    elif kind == "state":
        if stage not in stage_order:
            raise ContractError("invalid stage in research path")
        stage_number = f"{stage_order.index(stage):02d}"
        expected = Path("research_runs/states") / f"request={request_generation_id}" / f"run={run_id}" / f"stage={stage_number}" / "manifest.json"
    elif kind == "result":
        if not isinstance(result_generation_id, str) or not _SHA256_PATTERN.fullmatch(result_generation_id):
            raise ContractError("invalid result generation in research path")
        expected = Path("research_runs/results") / f"request={request_generation_id}" / f"run={run_id}" / f"result={result_generation_id}" / "manifest.json"
    else:
        raise ContractError("unknown research layout kind")
    candidate = Path(path)
    try:
        relative = candidate.relative_to(data_root.resolve()) if candidate.is_absolute() else candidate
    except ValueError as exc:
        raise ContractError("research path escapes data root") from exc
    relative = _validate_layout_common(relative)
    if relative != expected:
        raise ContractError(f"research path does not match {kind} layout")
    if candidate.exists() or candidate.is_symlink():
        raise ContractError("research path overwrite is rejected")
    if not candidate.parent.exists() or candidate.parent.is_symlink():
        raise ContractError("research path parent is missing or symlinked")
    parent_resolved = candidate.parent.resolve(strict=True)
    root_resolved = data_root.resolve(strict=True)
    try:
        contained = parent_resolved.is_relative_to(root_resolved)
    except ValueError:
        contained = False
    if not contained:
        raise ContractError("research path escapes data root")
    return candidate


def validate_provider_config_ref(config_ref: str, *, trust_root: Path, registered_names: set[str]) -> ProviderConfig:
    """Validate a registered relative provider config path against the CLI trust root."""
    if "\\" in config_ref:
        raise ContractError("provider configuration path must not contain backslashes")
    candidate = Path(config_ref)
    if candidate.is_absolute():
        raise ContractError("provider configuration path must be relative")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ContractError("provider configuration path must not traverse")
    if any(part.startswith(".") for part in candidate.parts):
        raise ContractError("provider configuration path must not contain hidden segments")
    resolved = (trust_root / candidate).resolve(strict=True)
    trust_resolved = trust_root.resolve(strict=True)
    try:
        contained = resolved.is_relative_to(trust_resolved)
    except ValueError:
        contained = False
    if not contained or not resolved.is_file():
        raise ContractError("provider configuration is missing or escapes trust root")
    if config_ref not in registered_names:
        raise ContractError("provider configuration is not registered")
    return ProviderConfig(provider_id=config_ref, trust_anchor_id=config_ref, config_path=str(resolved))
