from __future__ import annotations

from pathlib import Path
import re


class StaticUniverseLoader:
    pattern = re.compile(r"^\d{6}\.(XSHG|XSHE)$")

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[str]:
        instruments = [
            item.strip()
            for item in self.path.read_text(encoding="utf-8").splitlines()
            if item.strip() and not item.lstrip().startswith("#")
        ]
        bad = [item for item in instruments if not self.pattern.fullmatch(item)]
        if bad:
            raise ValueError(f"invalid canonical instruments: {bad}")
        if len(set(instruments)) != len(instruments):
            raise ValueError("universe contains duplicates")
        return instruments
