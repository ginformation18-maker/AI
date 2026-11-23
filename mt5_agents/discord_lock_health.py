"""Manual, fail-closed inspection of the Discord observer process lock.

Never remove an active/unknown lock. Cleanup requires a separate explicit CLI
flag after stopping bot instances; no automatic startup bypass is implemented.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from .settings import ROOT

LOCK = ROOT / 'runtime' / 'discord_hub.lock'


def inspect_lock(lock: Path = LOCK, *, pid_exists=None) -> dict:
    if not lock.exists():
        return {'status': 'ABSENT', 'action': 'No existing observer lock'}
    try:
        before = lock.stat()
        original = lock.read_text(encoding='utf-8')
        if len(original) > 500:
            raise ValueError('unexpected lock content')
        matched = re.fullmatch(r'pid=(\d+) at=([^\r\n]+)\n?', original)
        if not matched:
            raise ValueError('unexpected lock format')
        pid = int(matched.group(1))
        if pid <= 0:
            raise ValueError('invalid PID')
        if pid_exists is None:
            import psutil
            pid_exists = psutil.pid_exists
        alive = pid_exists(pid)
    except (OSError, UnicodeError, ValueError, ImportError) as exc:
        return {'status': 'UNKNOWN', 'reason': type(exc).__name__,
                'action': 'Do not delete; inspect manually'}
    return {'status': 'ACTIVE_PID' if alive else 'STALE_CANDIDATE', 'pid': pid,
            'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
            'action': 'Stop this process before removing lock' if alive else
                      'Lock may be stale; explicit --clear-stale allowed after stopping all bot instances'}


def clear_stale_lock(lock: Path = LOCK, *, pid_exists=None) -> dict:
    state = inspect_lock(lock, pid_exists=pid_exists)
    if state['status'] == 'ABSENT':
        return state
    if state['status'] != 'STALE_CANDIDATE':
        return {'status': 'REFUSED', 'reason': state['status'],
                'action': 'No lock was removed'}
    # Recheck immediately before removing; race/unknown state fails closed.
    current = inspect_lock(lock, pid_exists=pid_exists)
    if (current['status'] != 'STALE_CANDIDATE' or
        current.get('pid') != state['pid'] or
        current.get('size') != state['size'] or
        current.get('mtime_ns') != state['mtime_ns']):
        return {'status': 'REFUSED', 'reason': 'LOCK_CHANGED',
                'action': 'No lock was removed'}
    try:
        lock.unlink()
    except OSError as exc:
        return {'status': 'REFUSED', 'reason': type(exc).__name__,
                'action': 'No lock was removed'}
    return {'status': 'REMOVED_STALE', 'pid': state['pid'],
            'action': 'Rerun one bot instance; keep MODE=DRY_RUN'}


def main() -> int:
    parser = argparse.ArgumentParser(description='Safe inspection of Discord observer lock')
    parser.add_argument('--clear-stale', action='store_true',
                        help='Only after closing ALL bot processes; never removes a live PID lock')
    opts = parser.parse_args()
    result = clear_stale_lock() if opts.clear_stale else inspect_lock()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('ABSENT', 'STALE_CANDIDATE', 'REMOVED_STALE') else 2


if __name__ == '__main__':
    raise SystemExit(main())
