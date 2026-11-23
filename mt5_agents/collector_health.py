"""External health check; nonzero exit for stale/missing heartbeat or unverified window."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def check_health(root: str | Path, symbol: str, *, max_age_seconds=180) -> dict:
    if not (30 <= max_age_seconds <= 3600):
        raise ValueError("Unsupported health age threshold")
    p=Path(root) / f"collector_health_{symbol}.json"
    if not p.is_file():
        raise RuntimeError("Collector has not committed a single window yet")
    item=json.loads(p.read_text(encoding="utf-8"))
    observed=datetime.fromisoformat(item["last_commit_utc"])
    age=(datetime.now(timezone.utc)-observed.astimezone(timezone.utc)).total_seconds()
    if age < -5 or age>max_age_seconds:
        raise RuntimeError(f"Stale/future heartbeat, age={age:.1f} sec")
    if item["last_state"] != "ARCHIVED":
        raise RuntimeError("Last window was NO_TICKS_UNVERIFIED, NOT verified coverage")
    return {"status":"HEALTHY_LATEST_WINDOW_ONLY", "age_seconds":round(age,1),
            "rss_bytes":item["rss_bytes"], "last_window_end_ms":item["last_window_end_ms"],
            "warning":"This health check is not a substitute for full session audit"}


def main():
    p=argparse.ArgumentParser(description="Monitor collection progress / heartbeat (read-only)")
    p.add_argument("--archive-root",default="runtime/tick_archive")
    p.add_argument("--symbol",required=True)
    p.add_argument("--max-age-seconds",type=int,default=180)
    args=p.parse_args()
    try:
        print(json.dumps(check_health(args.archive_root,args.symbol,max_age_seconds=args.max_age_seconds)))
    except Exception as exc:
        print(json.dumps({"status":"ALERT","error":f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(2) from exc


if __name__=="__main__":
    main()
