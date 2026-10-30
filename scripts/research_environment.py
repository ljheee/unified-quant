#!/usr/bin/env python3
"""Print observed bindings to use before signing a new research request.

No artifacts, credentials or vendor APIs are read. Redirect stdout to archive
the environment receipt; copy its two digests into the request's environment.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.research_chain.provenance import capture_environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(capture_environment(args.project_root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
