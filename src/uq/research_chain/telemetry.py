"""Attempt-local measurements, separate from deterministic result identities."""

from __future__ import annotations

import os
import math
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..contracts.canonical_v2 import fsync_dir
from ..contracts.model_layer import canonical_json, sha256_json
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError


class RunTelemetry:
    """Persist one immutable attempt receipt even when a stage fails.

    A resumed stage is reported as reused, never as a zero-duration execution.
    Durations are monotonic wall time and must not be compared to old ledgers'
    created_at fields, which are logical timestamps rather than measurements.
    """

    def __init__(
        self, root: Path, request: Mapping[str, Any], environment: Mapping[str, Any]
    ) -> None:
        self.root = Path(root)
        generation = request["request_content_generation_id"]
        run_id = request["run_id"]
        if not isinstance(generation, str) or len(generation) != 64 or any(
            char not in "0123456789abcdef" for char in generation
        ):
            raise ContractError("invalid telemetry request generation")
        try:
            if str(uuid.UUID(run_id)) != run_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContractError("invalid telemetry run id") from exc
        self.path = (
            self.root / "research_runs" / "telemetry" / f"request={generation}"
            / f"run={run_id}" / f"attempt={uuid.uuid4()}.json"
        )
        reject_symlinks_within(self.root, self.path, label="research telemetry")
        self.document = {
            "record_version": 1, "kind": "research_attempt_telemetry",
            "request_content_generation_id": generation, "run_id": run_id,
            "data_root": str(self.root.absolute()), "environment": dict(environment),
            "started_at": datetime.now(timezone.utc).isoformat(), "stages": [],
        }
        self._started = time.perf_counter()

    def __enter__(self) -> RunTelemetry:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.document.update({
            "status": "passed" if exc is None else "failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.perf_counter() - self._started,
        })
        self.document["checksum_sha256"] = sha256_json(self.document)
        try:
            self._publish()
        except (OSError, ContractError) as publication_error:
            if exc is None:
                raise
            exc.add_note(f"telemetry publication failed: {type(publication_error).__name__}")
        return False

    @contextmanager
    def stage(self, stage: str) -> Iterator[None]:
        started = time.perf_counter()
        record = {"stage": stage, "status": "passed"}
        try:
            yield
        except BaseException as exc:
            record.update({"status": "failed", "error_type": type(exc).__name__})
            raise
        finally:
            record["elapsed_seconds"] = time.perf_counter() - started
            self.document["stages"].append(record)

    def reused(self, stage: str) -> None:
        self.document["stages"].append({"stage": stage, "status": "reused"})

    def substage(self, stage: str, elapsed_seconds: float, status: str) -> None:
        """Record exporter work without placing timings in logical artifacts."""
        if (not stage.startswith("qlib_export/") or status not in {"passed", "failed"}
                or not isinstance(elapsed_seconds, (int, float))
                or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
            raise ContractError("invalid research telemetry substage")
        self.document["stages"].append({
            "stage": stage, "status": status, "elapsed_seconds": elapsed_seconds,
        })

    def _publish(self) -> None:
        reject_symlinks_within(self.root, self.path, label="research telemetry")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".telemetry-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json(self.document) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, self.path)
            fsync_dir(self.path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
