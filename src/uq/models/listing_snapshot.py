"""Pin the security-master input that canonical bars do not carry.

This is a reproducibility input, not a historical PIT-universe certificate.
The label input checksum already covers the selected listing dates; preserving
the map makes those inputs recoverable without contacting the vendor again.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Mapping

from ..contracts.canonical_v2 import fsync_dir
from ..contracts.model_layer import canonical_json, sha256_json
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError


class ListingDateSnapshotStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path_for(self, generation_id: str) -> Path:
        if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{64}", generation_id):
            raise ContractError("invalid listing snapshot generation")
        path = self.root / "listing_date_snapshots" / f"generation={generation_id}.json"
        reject_symlinks_within(self.root, path, label="listing snapshot")
        return path

    def publish(self, listing_dates: Mapping[str, date], *, source: str) -> dict:
        document = {
            "record_version": 1,
            "kind": "listing_date_snapshot",
            "source": source,
            "listing_dates": {name: value.isoformat() for name, value in listing_dates.items()},
        }
        self._validate(document)
        generation = sha256_json(document)
        document["generation_id"] = generation
        path = self.path_for(generation)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Link a fully written file into place: concurrent identical publications
        # are idempotent, and neither a crash nor a race can overwrite a snapshot.
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json(document) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
                fsync_dir(path.parent)
            except FileExistsError:
                pass
            stored, _mapping = self.read(generation)
            if stored != document:
                raise ContractError("listing snapshot publication mismatch")
            return stored
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, generation_id: str) -> tuple[dict, dict[str, date]]:
        path = self.path_for(generation_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError("listing snapshot is unavailable or malformed") from exc
        if not isinstance(document, dict) or set(document) != {
            "record_version", "kind", "source", "listing_dates", "generation_id"
        }:
            raise ContractError("invalid listing snapshot fields")
        payload = {key: value for key, value in document.items() if key != "generation_id"}
        self._validate(payload)
        if document["generation_id"] != generation_id or sha256_json(payload) != generation_id:
            raise ContractError("listing snapshot checksum mismatch")
        return document, {name: date.fromisoformat(value) for name, value in document["listing_dates"].items()}

    @staticmethod
    def _validate(document: dict) -> None:
        if type(document.get("record_version")) is not int or document["record_version"] != 1:
            raise ContractError("unsupported listing snapshot version")
        if document.get("kind") != "listing_date_snapshot":
            raise ContractError("invalid listing snapshot kind")
        if not isinstance(document.get("source"), str) or not document["source"].strip():
            raise ContractError("listing snapshot requires a source")
        mapping = document.get("listing_dates")
        if not isinstance(mapping, dict) or not mapping:
            raise ContractError("listing snapshot requires listing dates")
        for instrument, value in mapping.items():
            if not isinstance(instrument, str) or not re.fullmatch(r"[0-9]{6}\.(XSHG|XSHE|XBSE)", instrument):
                raise ContractError("invalid listing snapshot instrument")
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError as exc:
                raise ContractError("invalid listing snapshot date") from exc
