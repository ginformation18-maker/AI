"""Out-of-band heartbeat for the Phase 3.6 Shadow collector.

Separate process. It NEVER imports MT5, never calls the broker, never alters
Parquet/manifest files, and never submits orders. Python 3.12 + psutil.

UTC timestamps are for display; elapsed-time judgments use monotonic_ns.
Missing collector process, missing quotes, delayed archival and OS sleep are
NOT synonymous: all are labeled separately and conservatively.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from collections import deque
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
TELEMETRY = ROOT / 'runtime' / 'telemetry'
SHADOW = ROOT / 'runtime' / 'shadow_tick_archive'
LOG = TELEMETRY / 'heartbeat.jsonl'
LOCK = TELEMETRY / '.shadow_watchdog.lock'


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _collector_command(argv: Any) -> bool:
    if not isinstance(argv, (list, tuple)) or not argv:
        return False
    items = [str(item).strip('"\'').replace('\\', '/').lower() for item in argv]
    return ('--loop' in items and
            any(x.endswith('/phase36_shadow_capture.py') or x == 'phase36_shadow_capture.py' for x in items))


def process_matches_root(record: dict, root: Path) -> bool:
    """Match only the user's project, not another similarly named script."""
    if not _collector_command(record.get('cmdline')):
        return False
    cwd = record.get('cwd')
    if cwd:
        try:
            if Path(cwd).resolve() == root.resolve():
                return True
        except (OSError, ValueError):
            pass
    for item in record.get('cmdline', []):
        item = str(item).strip('"\'')
        if item.replace('\\', '/').lower().endswith('phase36_shadow_capture.py'):
            path = Path(item)
            if path.is_absolute():
                try:
                    return path.resolve() == (root / 'scripts' / 'phase36_shadow_capture.py').resolve()
                except (OSError, ValueError):
                    return False
    return False


def scan_processes(root: Path = ROOT) -> dict:
    """Only OS process metadata; never import or connect to MetaTrader5."""
    try:
        import psutil
    except ImportError:
        return {'collector_alive_bool': None, 'collector_pids': [], 'collector_root_count': 0,
                'collector_process_scan': 'PSUTIL_NOT_INSTALLED'}
    matched, incomplete = [], False
    try:
        for proc in psutil.process_iter(attrs=['pid', 'ppid', 'name', 'cmdline'], ad_value=None):
            row = proc.info
            name = str(row.get('name') or '').lower()
            if not (name.startswith('python') or name.startswith('py.exe')):
                continue
            if row.get('cmdline') is None:
                incomplete = True
                continue
            if not _collector_command(row['cmdline']):
                continue
            try:
                row = dict(row, cwd=proc.cwd())
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                row = dict(row, cwd=None)
            if process_matches_root(row, root):
                matched.append({'pid': int(row['pid']), 'ppid': int(row['ppid'] or 0)})
            elif row['cmdline'] is not None and row.get('cwd') is None:
                incomplete = True
    except (OSError, psutil.Error) as exc:
        return {'collector_alive_bool': None, 'collector_pids': [], 'collector_root_count': 0,
                'collector_process_scan': f'PROCESS_SCAN_ERROR:{type(exc).__name__}'}
    pids = {x['pid'] for x in matched}
    roots = [x['pid'] for x in matched if x['ppid'] not in pids]
    return {'collector_alive_bool': bool(matched) if not incomplete else (True if matched else None),
            'collector_pids': sorted(pids), 'collector_root_count': len(roots),
            'collector_process_scan': 'COMPLETE' if not incomplete else 'PARTIAL_PERMISSIONS'}


def latest_saved_batch(root: Path = SHADOW) -> dict:
    """Read manifest metadata only. No checksum load or changes to raw files."""
    data_dir = root / 'parquet'
    if not data_dir.exists():
        return {'last_saved_batch_id': None, 'last_saved_utc_ms': None,
                'last_manifest_scan': 'NO_SHADOW_ARCHIVE'}
    latest_time, latest = -1, None
    errors = 0
    for p in data_dir.rglob('*.manifest.json'):
        try:
            # Read only a small manifest, never a Parquet file.
            record = json.loads(p.read_text(encoding='utf-8'))
            observed = int(record['host_response_end_utc_ms'])
            batch_id = record['capture_batch_id']
            if not isinstance(batch_id, str) or len(batch_id) > 100:
                raise ValueError('invalid batch id')
            if observed > latest_time:
                latest, latest_time = batch_id, observed
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            errors += 1
    return {'last_saved_batch_id': latest, 'last_saved_utc_ms': latest_time if latest is not None else None,
            'last_manifest_scan': 'OK' if not errors else f'MANIFEST_READ_ERRORS:{errors}'}


def classify_interval(previous_monotonic_ns: int | None, now_monotonic_ns: int,
                      interval_seconds: float, previous_wall_ns: int | None = None,
                      wall_ns: int | None = None) -> dict:
    if previous_monotonic_ns is None:
        return {'watchdog_interval_ms': None, 'watchdog_interval_overrun_bool': False,
                'wall_vs_monotonic_shift_ms': None, 'wall_clock_shift_suspected_bool': False}
    elapsed_ns = now_monotonic_ns - previous_monotonic_ns
    elapsed_ms = round(elapsed_ns / 1e6, 3)
    shift = (round(((wall_ns - previous_wall_ns) - elapsed_ns) / 1e6, 3)
             if wall_ns is not None and previous_wall_ns is not None else None)
    return {'watchdog_interval_ms': elapsed_ms,
            'watchdog_interval_overrun_bool': elapsed_ms > interval_seconds * 1500,
            'wall_vs_monotonic_shift_ms': shift,
            'wall_clock_shift_suspected_bool': bool(shift is not None and abs(shift) > 3000)}


def build_heartbeat(now: datetime, mono_ns: int, wall_ns: int, proc: dict, batch: dict,
                    interval: dict) -> dict:
    current_ms = int(now.timestamp() * 1000)
    last_ms = batch.get('last_saved_utc_ms')
    lag_ms = current_ms - last_ms if isinstance(last_ms, int) else None
    flags = []
    if proc.get('collector_alive_bool') is False:
        flags.append('COLLECTOR_PROCESS_NOT_FOUND')
    elif proc.get('collector_alive_bool') is None:
        flags.append('COLLECTOR_PROCESS_STATUS_UNKNOWN')
    if proc.get('collector_root_count', 0) > 1:
        flags.append('MULTIPLE_INDEPENDENT_COLLECTORS_POSSIBLE')
    if interval['watchdog_interval_overrun_bool']:
        flags.append('WATCHDOG_CADENCE_DELAY_UNCLASSIFIED')
    if interval['wall_clock_shift_suspected_bool']:
        flags.append('HOST_CLOCK_STEP_SUSPECTED')
    if batch['last_manifest_scan'] != 'OK':
        flags.append(batch['last_manifest_scan'])
    if lag_ms is not None and lag_ms > 180_000:
        flags.append('NO_RECENT_SAVED_BATCH_MARKET_OR_CAPTURE_UNCLASSIFIED')
    if lag_ms is not None and lag_ms < -5_000:
        flags.append('SAVED_BATCH_FROM_FUTURE_HOST_CLOCK_CHECK')
    return {
        'schema_version': 'shadow-watchdog-v1', 'utc_iso': now.isoformat(timespec='milliseconds'),
        'host_utc_msc': current_ms, 'monotonic_ns': mono_ns,
        'collector_alive_bool': proc.get('collector_alive_bool'),
        'collector_pids': proc.get('collector_pids', []),
        'collector_root_count': proc.get('collector_root_count', 0),
        'collector_process_scan': proc.get('collector_process_scan'),
        # Refuse to fabricate MT5 connection state from process existence.
        'mt5_connected_bool': None, 'mt5_connection_check': 'NOT_PROBED_NO_MT5_CALLS',
        'last_saved_batch_id': batch['last_saved_batch_id'],
        'last_saved_utc_msc': last_ms, 'lag_ms': lag_ms,
        'lag_definition': 'host_utc_now_minus_last_successful_manifest_response_time',
        **interval, 'alerts': flags,
        'timebase_status': 'UNVERIFIED_PENDING_DST',
        'no_orders': True, 'original_parquet_unchanged': True,
    }


def _last_line(path: Path) -> dict | None:
    if not path.is_file():
        return None
    # File is line-appended. Use bounded deque; a daily file is usually small.
    with path.open('r', encoding='utf-8') as f:
        for line in reversed(deque(f, maxlen=3)):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _rotate_if_needed(path: Path, today: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    last = _last_line(path)
    if last is None:
        raise RuntimeError('Invalid heartbeat log: manual inspection required')
    prev = str(last.get('utc_iso', ''))[:10]
    if prev == today:
        return
    if not (len(prev) == 10 and prev[4] == '-' and prev[7] == '-'):
        raise RuntimeError('Heartbeat log date not parseable; no rotation')
    rotated = path.with_name(f'heartbeat-{prev}.jsonl')
    if rotated.exists():
        raise RuntimeError('Daily rotation destination exists; do not overwrite history')
    path.rename(rotated)


def append_heartbeat(record: dict, path: Path = LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path, record['utc_iso'][:10])
    with path.open('a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        f.flush()
        os.fsync(f.fileno())


@contextmanager
def single_watchdog(lock: Path = LOCK):
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError('Watchdog lock exists: inspect first; do not delete while running') from exc
    marker = f'pid={os.getpid()}\n'
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(marker)
            stream.flush()
        yield
    finally:
        if lock.exists() and lock.read_text(encoding='utf-8') == marker:
            lock.unlink()


def report_status(path: Path = LOG, lock: Path = LOCK) -> dict:
    record = _last_line(path)
    now = utc_now()
    stamp = (datetime.fromisoformat(record['utc_iso']) if record else None)
    age = (now - stamp).total_seconds() if stamp else None
    lock_pid = None
    lock_exists = lock.exists()
    if lock_exists:
        try:
            line = lock.read_text(encoding='utf-8').strip()
            lock_pid = int(line.split('=', 1)[-1]) if line.startswith('pid=') else None
        except (OSError, ValueError):
            pass
    return {'status': 'NO_HEARTBEAT_YET' if record is None else ('RECENT' if age is not None and 0 <= age < 35 else 'STALE_OR_CLOCK_ADJUSTED'),
            'last_heartbeat_age_seconds': round(age, 3) if age is not None else None,
            'watchdog_lock_exists': lock_exists, 'watchdog_lock_pid': lock_pid,
            'last_heartbeat': record}


def sample_once(previous_mono: int | None, previous_wall: int | None,
                interval_seconds: float, *, scan: dict | None = None) -> tuple[dict, int, int]:
    mono = time.monotonic_ns()
    wall = time.time_ns()
    interval = classify_interval(previous_mono, mono, interval_seconds, previous_wall, wall)
    proc = scan_processes()
    if scan is None:
        scan = latest_saved_batch()
    record = build_heartbeat(utc_now(), mono, wall, proc, scan, interval)
    return record, mono, wall


def main() -> None:
    parser = argparse.ArgumentParser(description='Independent read-only Shadow collector watchdog; NO MT5 and NO orders')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--once', action='store_true', help='Write one telemetry record (NO collector restart)')
    group.add_argument('--loop', action='store_true', help='Independent 10-second heartbeat loop')
    group.add_argument('--status', action='store_true', help='Read last telemetry record without writing')
    parser.add_argument('--interval-seconds', type=float, default=10)
    parser.add_argument('--manifest-scan-seconds', type=float, default=60)
    args = parser.parse_args()
    if args.status:
        print(json.dumps(report_status(), ensure_ascii=False, indent=2), flush=True)
        return
    if not 2 <= args.interval_seconds <= 300:
        parser.error('interval-seconds must be between 2 and 300')
    if args.manifest_scan_seconds < args.interval_seconds:
        parser.error('manifest-scan-seconds must be >= interval-seconds')
    if args.once:
        record, _, _ = sample_once(None, None, args.interval_seconds)
        append_heartbeat(record)
        print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
        return
    with single_watchdog():
        print('WATCHDOG STARTED: independent process, heartbeat every %s sec, NO MT5/NO orders' % args.interval_seconds, flush=True)
        prev_mono, prev_wall = None, None
        cached = None
        next_scan_mono = 0
        while True:
            loop_start_mono = time.monotonic_ns()
            if cached is None or loop_start_mono >= next_scan_mono:
                cached = latest_saved_batch()
                next_scan_mono = loop_start_mono + int(args.manifest_scan_seconds * 1e9)
            record, prev_mono, prev_wall = sample_once(prev_mono, prev_wall, args.interval_seconds, scan=cached)
            append_heartbeat(record)
            if record['alerts']:
                print(json.dumps({'utc_iso': record['utc_iso'], 'alerts': record['alerts']}, ensure_ascii=False), flush=True)
            remaining = args.interval_seconds - (time.monotonic_ns() - loop_start_mono) / 1e9
            time.sleep(max(0.0, remaining))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('WATCHDOG STOPPED; Shadow collector was not touched.', flush=True)
    except Exception as exc:
        print(f'WATCHDOG ERROR: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
