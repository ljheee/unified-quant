from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="apply local research data retention policy")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--raw-days", type=int, default=30)
    parser.add_argument("--quarantine-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for name, days in (("raw", args.raw_days), ("quarantine", args.quarantine_days)):
        directory = args.data_root / name
        cutoff = time.time() - days * 86400
        expired = sorted(path for path in directory.glob("*") if path.stat().st_mtime < cutoff) if directory.exists() else []
        print(f"{name}: {len(expired)} expired")
        if not args.dry_run:
            for path in expired:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()

    canonical = args.data_root / "canonical"
    stale_staging = list(canonical.rglob("*.staging.*")) if canonical.exists() else []
    print(f"staging: {len(stale_staging)} stale")
    if not args.dry_run:
        for path in stale_staging:
            shutil.rmtree(path, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
