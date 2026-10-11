from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import tushare as ts

INDEX_CODE = "000510.SH"


def fingerprints(client, start: str, end: str) -> dict[str, str]:
    raw = client.index_weight(index_code=INDEX_CODE, start_date=start, end_date=end)
    result: dict[str, str] = {}
    for trade_date, group in raw.groupby("trade_date"):
        rows = sorted(zip(group["con_code"], group["weight"].round(6)))
        result[str(trade_date)] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="record first-seen time of each index_weight publish date")
    parser.add_argument("--start", default="20250101")
    parser.add_argument("--end", default=dt.date.today().strftime("%Y%m%d"))
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise SystemExit("TUSHARE_TOKEN is not set")
    client = ts.pro_api(token)
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    current = fingerprints(client, args.start, args.end)

    seen: dict[str, str] = {}
    if args.log.exists():
        for line in args.log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            seen.setdefault(record["trade_date"], record["fingerprint"])
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("a", encoding="utf-8") as handle:
        for trade_date, fingerprint in sorted(current.items()):
            if seen.get(trade_date) == fingerprint:
                continue
            event = "first_seen" if trade_date not in seen else "content_changed"
            handle.write(json.dumps({
                "observed_at": observed_at,
                "trade_date": trade_date,
                "fingerprint": fingerprint,
                "event": event,
            }, sort_keys=True) + "\n")
    print(json.dumps({"observed_at": observed_at, "dates": len(current)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
