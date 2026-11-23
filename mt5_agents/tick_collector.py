"""Phase 3: bounded, SEPARATE-PROCESS MT5 tick collection to daily ZSTD Parquet.

Never imported by the trading polling loop. No calls to order_send.
Each UTC window is half-open [start, end), regardless of MT5's inclusive range
boundary. Gaps are explicitly unverified, never fabricated as flat quotes.

A standalone SQLite database contains only small archival MANIFEST metadata,
not tick rows. Manifest commit follows an atomic Parquet rename. A crash
between rename and commit leaves an orphan that requires manual verification:
we do not silently overwrite it or skip an unindexed file.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from uuid import uuid4

import pandas as pd

from .tick_data import TickQualityError, normalize_ticks, _utc_ms
from .parquet_integrity import write_verified_parquet, verify_parquet

MAX_WINDOW_SECONDS = 120
MAX_TICKS_PER_WINDOW = 250_000
MIN_FINALITY_LAG_SECONDS = 5


class TransientCollectorConnectionError(TickQualityError):
    """Only explicitly classified data-source failures may trigger guarded restart."""


def _write_health(archive_root: Path, result: dict, window_end_ms: int) -> None:
    """Durable per-window health marker; absent/stale markers are operator alarms."""
    import psutil
    payload = {
        "last_commit_utc": datetime.now(timezone.utc).isoformat(),
        "last_window_end_ms": int(window_end_ms),
        "last_state": result["state"],
        "symbol": result["symbol"],
        "last_count": int(result.get("count", 0)),
        "rss_bytes": int(psutil.Process().memory_info().rss),
        "pid": os.getpid(),
        "note": "NO_TICKS_UNVERIFIED is a gap, not successful tick coverage",
    }
    dest = archive_root / f"collector_health_{_safe_symbol(result['symbol'])}.json"
    tmp = dest.with_name(dest.name + "." + uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fd:
            json.dump(payload,fd,ensure_ascii=False,indent=2)
            fd.flush()
            os.fsync(fd.fileno())
        os.replace(tmp,dest)
        # RSS time series for ten-session SOAK diagnostics. Not tick data;
        # append-only, one small line per collector window. fsync after write.
        history = archive_root / f"collector_health_history_{_safe_symbol(result['symbol'])}.jsonl"
        with history.open("a", encoding="utf-8") as fd:
            fd.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            fd.flush()
            os.fsync(fd.fileno())
    finally:
        tmp.unlink(missing_ok=True)


def _safe_symbol(symbol: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.#-]{1,40}", symbol):
        raise ValueError("Symbol contains unsafe filesystem characters")
    return symbol


def _utc_time(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class TickArchive:
    """Idempotent writer with explicit metadata checks and no historical backfill in trading loop."""

    def __init__(self, root: str | Path, broker: Any, account_login: int, broker_server: str):
        self.root = Path(root).resolve()
        self.mt5 = broker
        if account_login <= 0 or not broker_server.strip():
            raise ValueError("Verified broker login and server required")
        self.login = int(account_login)
        self.server = broker_server.strip()
        self.archive = self.root / "parquet"
        self.index_path = self.root / "tick_manifest.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tick_windows (
                account_login INTEGER NOT NULL, broker_server TEXT NOT NULL,
                symbol TEXT NOT NULL, window_start_ms INTEGER NOT NULL,
                window_end_ms INTEGER NOT NULL, state TEXT NOT NULL
                    CHECK(state IN ('ARCHIVED','NO_TICKS_UNVERIFIED')),
                relative_path TEXT, count INTEGER NOT NULL,
                first_tick_ms INTEGER, last_tick_ms INTEGER,
                point REAL NOT NULL, bytes INTEGER, sha256 TEXT,
                recorded_utc TEXT NOT NULL,
                PRIMARY KEY(account_login, broker_server, symbol, window_start_ms)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_tick_windows_resume
                       ON tick_windows(account_login,broker_server,symbol,window_end_ms)""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.index_path), timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _exclusive(self, symbol: str):
        """One collector per account/server/symbol. A crash leaves a forensic lock."""
        server_tag = hashlib.sha256(self.server.encode()).hexdigest()[:12]
        locks = self.root / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock = locks / f"login_{self.login}_server_{server_tag}_symbol_{symbol}.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("Another collector or stale crash lock exists; verify state manually") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"pid={os.getpid()} utc={datetime.now(timezone.utc).isoformat()}\n")
                f.flush()
                os.fsync(f.fileno())
            yield
        finally:
            lock.unlink(missing_ok=True)

    def latest_end(self, symbol: str) -> int | None:
        symbol = _safe_symbol(symbol)
        with self._db() as db:
            result = db.execute("""SELECT MAX(window_end_ms) FROM tick_windows
                    WHERE account_login=? AND broker_server=? AND symbol=?""",
                    (self.login, self.server, symbol)).fetchone()[0]
        return int(result) if result is not None else None

    def _path_for(self, symbol: str, start: datetime, end: datetime) -> Path:
        # No window may straddle UTC midnight. This keeps partitions truly daily.
        if (end - timedelta(microseconds=1)).date() != start.date():
            raise ValueError("Window crosses UTC date boundary; split it first")
        label = hashlib.sha256(self.server.encode("utf-8")).hexdigest()[:12]
        return (self.archive / f"login={self.login}" / f"server={label}" /
                f"symbol={symbol}" / f"date={start:%Y-%m-%d}" /
                f"ticks_{_utc_ms(start)}_{_utc_ms(end)}.parquet")

    def collect(self, symbol: str, start: datetime, end: datetime) -> dict:
        """Fetch at most 120 seconds in a mutually exclusive archival transaction."""
        symbol = _safe_symbol(symbol)
        with self._exclusive(symbol):
            return self._collect_locked(symbol, start, end)

    def _collect_locked(self, symbol: str, start: datetime, end: datetime) -> dict:
        start_ms, end_ms = _utc_ms(start), _utc_ms(end)
        if end > datetime.now(timezone.utc) - timedelta(seconds=MIN_FINALITY_LAG_SECONDS):
            raise ValueError("Window not fully closed and old enough for broker finality lag")
        duration = (end_ms - start_ms) / 1000
        if not (0 < duration <= MAX_WINDOW_SECONDS):
            raise ValueError(f"Window must be >0 and <= {MAX_WINDOW_SECONDS} seconds")
        path = self._path_for(symbol, start, end)
        account = self.mt5.account_info()
        if account is None:
            raise TransientCollectorConnectionError("MT5 account_info returned None: no checkpoint; guarded restart allowed")
        if (int(getattr(account, "login", -1)) != self.login
                or str(getattr(account, "server", "")) != self.server):
            raise RuntimeError("Broker identity changed: refusing to mix tick archives")
        info = self.mt5.symbol_info(symbol)
        if info is None or float(info.point) <= 0:
            raise RuntimeError(f"Invalid symbol info for {symbol}")
        point = float(info.point)
        with self._db() as db:
            old = db.execute("""SELECT state,relative_path,sha256,window_end_ms,count
                    FROM tick_windows WHERE account_login=? AND broker_server=?
                    AND symbol=? AND window_start_ms=?""",
                    (self.login, self.server, symbol, start_ms)).fetchone()
        if old:
            state, relative, sha, recorded_end, recorded_count = old
            if recorded_end != end_ms:
                raise RuntimeError("Conflicting window already recorded")
            if state == "ARCHIVED":
                previous = self.root / relative
                if not previous.is_file() or _checksum(previous) != sha:
                    raise RuntimeError("Tick archive file missing or checksum mismatch; manual repair required")
                verify_parquet(previous)  # even a matching hash is not proof of readable Parquet
            return {"state": state, "symbol": symbol, "start": start.isoformat(),
                    "count": int(recorded_count), "already_indexed": True}
        with self._db() as db:
            overlaps = db.execute("""SELECT COUNT(*) FROM tick_windows
                WHERE account_login=? AND broker_server=? AND symbol=?
                AND window_start_ms < ? AND window_end_ms > ?""",
                (self.login, self.server, symbol, end_ms, start_ms)).fetchone()[0]
        if overlaps:
            raise RuntimeError("Overlapping tick archive window; do not duplicate source observations")
        if path.exists():
            raise RuntimeError("Orphan Parquet with no manifest row; inspect manually, never overwrite")
        raw = self.mt5.copy_ticks_range(symbol, start, end, self.mt5.COPY_TICKS_INFO)
        if raw is None:
            raise TransientCollectorConnectionError("MT5 returned None: no checkpoint recorded; guarded restart allowed")
        if len(raw) > MAX_TICKS_PER_WINDOW:
            raise TickQualityError("Tick limit exceeded; split the separate collector interval")
        # Some MT5 versions return the right boundary inclusively. Store only [start,end).
        if len(raw):
            frame = normalize_ticks(raw, start_utc=start, end_utc=end, point=point)
            frame = frame.loc[(frame.time_msc >= start_ms) & (frame.time_msc < end_ms),
                              ["time_msc", "bid", "ask", "flags", "spread_points"] if "flags" in frame.columns
                              else ["time_msc", "bid", "ask", "spread_points"]].copy()
        else:
            frame = pd.DataFrame()
        now = datetime.now(timezone.utc).isoformat()
        if frame.empty:
            # An empty MT5 response cannot prove the market was closed. Keep a gap.
            self._record(symbol, start_ms, end_ms, "NO_TICKS_UNVERIFIED", None, 0,
                         None, None, point, None, None, now)
            return {"state": "NO_TICKS_UNVERIFIED", "symbol": symbol,
                    "start": start.isoformat(), "count": 0}
        # Parquet dependency is intentionally mandatory: no CSV fallback or SQLite tick insert.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{uuid4().hex}.tmp")
        try:
            write_verified_parquet(frame, tmp)  # REQUIRED real readback before atomic publish
            with tmp.open("rb+") as f:
                os.fsync(f.fileno())
            # Replace only after a fresh existence check. Use one collector per account/symbol.
            if path.exists():
                raise RuntimeError("Concurrent collector detected; abort without overwrite")
            os.replace(tmp, path)
            # If this metadata transaction fails, the orphan file MUST be reviewed.
            self._record(symbol, start_ms, end_ms, "ARCHIVED",
                         str(path.relative_to(self.root)), len(frame),
                         int(frame.time_msc.iloc[0]), int(frame.time_msc.iloc[-1]),
                         point, path.stat().st_size, _checksum(path), now)
        finally:
            tmp.unlink(missing_ok=True)
        return {"state": "ARCHIVED", "symbol": symbol, "start": start.isoformat(),
                "count": len(frame), "path": str(path)}

    def _record(self, symbol, start_ms, end_ms, state, relative, count,
                first, last, point, file_bytes, sha, at):
        with self._db() as db:
            db.execute("""INSERT INTO tick_windows
                (account_login,broker_server,symbol,window_start_ms,window_end_ms,
                 state,relative_path,count,first_tick_ms,last_tick_ms,point,bytes,sha256,recorded_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.login,self.server,symbol,start_ms,end_ms,state,relative,count,
                 first,last,point,file_bytes,sha,at))


def last_closed_boundary(now: datetime, window_seconds: int, delay_seconds: int = 10) -> datetime:
    if not (10 <= window_seconds <= MAX_WINDOW_SECONDS) or not (MIN_FINALITY_LAG_SECONDS <= delay_seconds <= 60):
        raise ValueError("Unsafe collector window or finality lag")
    seconds = int(now.astimezone(timezone.utc).timestamp()) - delay_seconds
    return datetime.fromtimestamp(seconds // window_seconds * window_seconds, timezone.utc)


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError("Use an explicit UTC time, e.g. 2026-11-21T14:00:00Z")
    return dt


def main() -> None:
    from .market import connect_mt5
    from .settings import ROOT, Settings
    from .parquet_preflight import run_preflight
    import MetaTrader5 as mt5
    from dataclasses import replace

    parser = argparse.ArgumentParser(description="Standalone bounded MT5->daily Parquet tick collector (never sends orders)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--terminal-path", help="Dedicated terminal64.exe path for the collector (recommended)")
    parser.add_argument("--window-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true", help="Archive one bounded UTC window")
    parser.add_argument("--forever", action="store_true", help="Continuously archive completed recent windows")
    parser.add_argument("--start-utc", help="For --once: explicit UTC window start")
    parser.add_argument("--max-backlog-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.once == args.forever:
        parser.error("Choose exactly one of --once or --forever")
    if args.start_utc and not args.once:
        parser.error("--start-utc is for --once only")
    if not (10 <= args.window_seconds <= MAX_WINDOW_SECONDS and 0 < args.max_backlog_seconds <= 3600):
        parser.error("Unsafe collector settings")
    # Daily boundaries split naturally for windows dividing 86400 seconds.
    if 86400 % args.window_seconds:
        parser.error("Window must evenly divide 86400 to prevent UTC partition splits")
    # Never archive timestamps in the old canonical UTC schema while a
    # broker offset remains only a provisional observation hypothesis.
    from .observation_timebase import require_unshifted_archive_timebase
    require_unshifted_archive_timebase()
    # No collector startup can bypass a REAL local pyarrow write/read test.
    # It checks the exact filesystem to be used for subsequent archive files.
    print(json.dumps(run_preflight(ROOT / "runtime" / "tick_archive"), ensure_ascii=False), flush=True)
    cfg = Settings.load()
    if args.terminal_path:
        cfg = replace(cfg, terminal_path=args.terminal_path)
    if cfg.mode == "LIVE":
        raise RuntimeError("Collector CLI only supports read-only DRY_RUN/DEMO setup, not LIVE")
    connected = False
    try:
        account = connect_mt5(cfg)
        connected = True
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("Observation collector requires verified DEMO account, even with MODE=DRY_RUN")
        archive = TickArchive(ROOT / "runtime" / "tick_archive", mt5, cfg.login, str(account.server))
        if args.once:
            end = last_closed_boundary(datetime.now(timezone.utc), args.window_seconds)
            start = _parse_utc(args.start_utc) if args.start_utc else end - timedelta(seconds=args.window_seconds)
            result = archive.collect(args.symbol, start, start + timedelta(seconds=args.window_seconds))
            _write_health(archive.root, result, _utc_ms(start) + args.window_seconds * 1000)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return
        cursor = archive.latest_end(args.symbol)
        while True:
            end_ms = _utc_ms(last_closed_boundary(datetime.now(timezone.utc), args.window_seconds))
            if cursor is None:
                cursor = end_ms - args.window_seconds * 1000
            if end_ms - cursor > args.max_backlog_seconds * 1000:
                raise RuntimeError("Collector backlog too large; stop and perform deliberate bounded backfill. No automatic multi-hour replay.")
            if cursor < end_ms:
                start = _utc_time(cursor)
                result = archive.collect(args.symbol, start, start + timedelta(seconds=args.window_seconds))
                # The manifest file and checksum must exist before health advances.
                _write_health(archive.root, result, cursor + args.window_seconds * 1000)
                print(json.dumps({"event":"TICK_WINDOW", **result}, ensure_ascii=False), flush=True)
                cursor += args.window_seconds * 1000
            else:
                time.sleep(5)
    finally:
        if connected:
            mt5.shutdown()


if __name__ == "__main__":
    try:
        main()
    except TransientCollectorConnectionError as exc:
        logging.error("TRANSIENT: %s", exc)
        raise SystemExit(75) from exc
    except RuntimeError as exc:
        # MT5 initialization may be transient. A limited supervisor can retry,
        # but may NEVER clear stale locks, rewrite gaps, or resubmit trades.
        if str(exc).startswith("MT5 initialize failed:"):
            logging.error("TRANSIENT MT5 INIT: %s", exc)
            raise SystemExit(75) from exc
        raise
