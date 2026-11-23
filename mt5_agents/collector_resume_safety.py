"""Fail-closed filesystem/manifest consistency gate before a supervised restart.

Do not remove stale lock files or orphaned Parquet files automatically; those
need forensic inspection. Confirms latest bounded number of archived windows,
not a replacement for the full five-session readback audit.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3

from .tick_collector import _checksum
from .parquet_integrity import verify_parquet


def verify_safe_resume(root: str | Path, *, recent_per_symbol: int = 10) -> dict:
    if not (1 <= recent_per_symbol <= 120):
        raise ValueError("recent_per_symbol must be 1..120")
    base = Path(root).resolve()
    locks = list((base / "locks").glob("*.lock"))
    if locks:
        raise RuntimeError(f"Possible live/stale collector locks: {len(locks)}; DO NOT auto-delete")
    tmpfiles = list((base / "parquet").rglob("*.tmp")) if (base / "parquet").is_dir() else []
    if tmpfiles:
        raise RuntimeError(f"Uncommitted Parquet temp file(s): {len(tmpfiles)}; manual review required")
    index = base / "tick_manifest.sqlite3"
    parquet_files = list((base / "parquet").rglob("*.parquet")) if (base / "parquet").is_dir() else []
    if not index.exists():
        if parquet_files:
            raise RuntimeError("Parquet files without manifest: cannot resume")
        return {"status":"SAFE_EMPTY_ARCHIVE", "recent_verified":0}
    with sqlite3.connect(f"file:{index.as_posix()}?mode=ro",uri=True) as db:
        outcome = db.execute("PRAGMA integrity_check").fetchone()
        if outcome != ("ok",):
            raise RuntimeError("SQLite manifest integrity_check failed")
        rows = db.execute("""SELECT account_login,broker_server,symbol,relative_path,
                              count,first_tick_ms,last_tick_ms,sha256
                              FROM tick_windows WHERE state='ARCHIVED'
                              ORDER BY window_start_ms DESC""").fetchall()
    # Full orphan scan is cheap relative to reading thousands of tick records.
    indexed = {str(r[3]).replace('\\','/') for r in rows if r[3]}
    actual = {str(p.relative_to(base)).replace('\\','/') for p in parquet_files}
    unindexed = actual-indexed
    missing = indexed-actual
    if unindexed or missing:
        raise RuntimeError(f"Parquet/manifest mismatch: orphan={len(unindexed)}, missing={len(missing)}")
    per_symbol = {}
    verified = 0
    for login, server, symbol, relative, count, first, last, digest in rows:
        key = (str(login), str(server), str(symbol))
        if per_symbol.get(key, 0) >= recent_per_symbol:
            continue
        # A separate counter avoids full-file readback on every startup.
        path = (base/relative).resolve()
        if not path.is_relative_to(base) or not digest or _checksum(path) != digest:
            raise RuntimeError("Recent Parquet checksum/path mismatch")
        info = verify_parquet(path)
        if (info["rows"],info["first_ms"],info["last_ms"]) != (count,first,last):
            raise RuntimeError("Recent Parquet contents differ from manifest")
        verified += 1
        per_symbol[key]=per_symbol.get(key,0)+1
    return {"status":"SAFE_TO_ATTEMPT_RESUME", "recent_verified":verified,
            "archive_files":len(actual),"caveat":"MT5 account and backlog checked separately after reconnect"}


def main():
    p=argparse.ArgumentParser(description="Check manifest/files before SAFE supervised collector restart")
    p.add_argument("--archive-root",default="runtime/tick_archive")
    args=p.parse_args()
    try:
        print(json.dumps(verify_safe_resume(args.archive_root),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status":"UNSAFE","error":f"{type(exc).__name__}: {exc}"},ensure_ascii=False))
        raise SystemExit(2) from exc


if __name__=="__main__":
    main()
