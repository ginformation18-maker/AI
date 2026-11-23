"""Offline five-session completeness/integrity gate; never trades or trains.

User must supply exact expected BROKER trading-session windows in UTC. The
checker never guesses holidays, early closes, or CFD maintenance breaks.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .tick_collector import _checksum
from .parquet_integrity import verify_parquet


def parse_utc(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError("All session boundaries must use explicit UTC (Z/+00:00)")
    return dt.astimezone(timezone.utc)


def audit(root: Path, schedule: dict, *, min_sessions: int = 5) -> dict:
    if min_sessions < 5:
        raise ValueError("Cannot lower the five-trading-session observation gate")
    symbol = schedule["symbol"]
    login = int(schedule["account_login"])
    server = str(schedule["broker_server"])
    window = int(schedule.get("window_seconds", 60))
    sessions = schedule["sessions"]
    if not (window in (10, 20, 30, 60, 120) and sessions and len(sessions) <= 30):
        raise ValueError("Unsafe or empty observation schedule")
    # Even a valid 1-minute interval isn't a whole trading session. Sessions
    # MUST be verified against the broker calendar by the human operator.
    dates = set()
    prev_end = None
    stats = []
    base = root.resolve()
    manifest = base / "tick_manifest.sqlite3"
    if not manifest.is_file():
        raise RuntimeError("Missing tick manifest. Collector has not completed archiving")
    with sqlite3.connect(f"file:{manifest.as_posix()}?mode=ro", uri=True) as db:
        for sess in sessions:
            start, end = parse_utc(sess["start_utc"]), parse_utc(sess["end_utc"])
            if not (end > start and end - start <= timedelta(hours=24)
                    and (end - start).total_seconds() >= 60 * 60
                    and int(start.timestamp()) % window == 0
                    and int(end.timestamp()) % window == 0):
                raise ValueError("Each scheduled session must be aligned, 1-24 hours long")
            if prev_end is not None and start < prev_end:
                raise ValueError("Overlapping or unordered scheduled sessions")
            prev_end = end
            dates.add(start.date().isoformat())
            lo, hi = int(start.timestamp()*1000), int(end.timestamp()*1000)
            rows = db.execute("""SELECT window_start_ms,window_end_ms,state,relative_path,count,
                first_tick_ms,last_tick_ms,sha256 FROM tick_windows
                WHERE account_login=? AND broker_server=? AND symbol=?
                AND window_start_ms>=? AND window_start_ms<? ORDER BY window_start_ms""",
                (login,server,symbol,lo,hi)).fetchall()
            by_start = {int(r[0]):r for r in rows}
            good, failures = 0, []
            for ms in range(lo, hi, window * 1000):
                row = by_start.get(ms)
                if row is None:
                    failures.append({"start_ms":ms,"issue":"MISSING_WINDOW"})
                    continue
                _, ending, state, relative, count, first, last, sha = row
                if (ending != ms + window*1000 or state != "ARCHIVED"
                        or not relative or count <= 0 or not sha
                        or first is None or last is None
                        or not (ms <= first <= last < ending)):
                    failures.append({"start_ms":ms,"issue":f"INVALID_{state}"})
                    continue
                path = (base / relative).resolve()
                if not path.is_relative_to(base) or not path.is_file() or _checksum(path) != sha:
                    failures.append({"start_ms":ms,"issue":"FILE_MISSING_OR_HASH_MISMATCH"})
                    continue
                try:
                    checked = verify_parquet(path)
                    if checked["rows"] != count or checked["first_ms"] != first or checked["last_ms"] != last:
                        raise RuntimeError("Manifest does not match decoded Parquet rows")
                except Exception as exc:
                    failures.append({"start_ms":ms,"issue":f"PARQUET_INVALID:{type(exc).__name__}"})
                    continue
                good += 1
            stats.append({"start_utc":start.isoformat(), "end_utc":end.isoformat(),
                          "expected_windows":(hi-lo)//(window*1000),
                          "verified_windows":good, "failures":failures,
                          "complete":not failures})
    all_good = len(dates) >= min_sessions and len(sessions) >= min_sessions and all(s["complete"] for s in stats)
    return {"ready_for_manual_review":all_good,
            "required_full_trading_days":min_sessions,
            "scheduled_unique_utc_dates":len(dates),
            "days":stats,
            "NOTE":"Five or ten sessions check collection PIPELINE STABILITY ONLY. No number of these sessions constitutes machine-learning training approval. Operator must verify full broker session hours, regimes, data integrity, and memory usage."}


def main():
    p = argparse.ArgumentParser(description="Verify stored ticks against broker session schedule; no trading or training")
    p.add_argument("--archive", required=True, help="runtime/tick_archive directory")
    p.add_argument("--schedule", required=True, help="JSON sessions in exact UTC with broker identity")
    p.add_argument("--required-days", type=int, default=5, choices=(5,10,14,15,20))
    p.add_argument("--out", help="Write JSON report to this path")
    args = p.parse_args()
    try:
        report = audit(Path(args.archive), json.loads(Path(args.schedule).read_text(encoding="utf-8")),
                       min_sessions=args.required_days)
    except Exception as exc:
        report = {"ready_for_manual_review":False,"error":f"{type(exc).__name__}: {exc}"}
    content = json.dumps(report, ensure_ascii=False, indent=2)
    print(content)
    if args.out:
        Path(args.out).write_text(content, encoding="utf-8")
    if not report["ready_for_manual_review"]:
        raise SystemExit(2)

if __name__ == "__main__":
    main()
