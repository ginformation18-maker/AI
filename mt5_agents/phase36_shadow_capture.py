"""Phase 3.6 shadow-only capture for MT5 broker-clock ticks.

No trading, no UTC conversion, no original-archive writes, no historical backfill.
All raw timestamps remain unmodified and are explicitly UNVERIFIED_PENDING_DST.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import numpy as np

STATUS = 'UNVERIFIED_PENDING_DST'
WINDOW_MS = 60_000
MAX_TICKS = 150_000
FINALITY_MS = 10_000
MAX_QUOTE_AGE_SECONDS = 10


def require_windows_ntp_sync() -> None:
    """Fail closed if Windows has no currently synchronized W32Time source.

    A successful status is not proof of subsecond clock precision; independent
    NTP stripchart/monitoring remains an operator requirement.
    """
    if os.name != 'nt':
        return  # Linux unit-test environment only; this collector is Windows-only.
    try:
        result = subprocess.run(['w32tm', '/query', '/status'], capture_output=True,
                                text=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Windows time sync status unavailable') from exc
    if (result.returncode != 0 or not re.search(r'Leap Indicator\s*:\s*0\b', result.stdout, re.I)
            or re.search(r'Source\s*:\s*(Local CMOS Clock|unspecified)', result.stdout, re.I)):
        raise RuntimeError('Windows Time is not confirmed synchronized: SHADOW CAPTURE BLOCKED')


def safe_symbol(symbol: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_.#-]{1,40}', symbol):
        raise ValueError('Unsafe broker symbol name')
    return symbol


def checked_offset(raw_tick_ms: int, host_ms: int, candidate_offset_sec: int,
                   max_quote_age_sec: int = MAX_QUOTE_AGE_SECONDS) -> float:
    """Observe freshness under a *candidate* shift; never certify UTC or DST."""
    observed_age_s = (host_ms + candidate_offset_sec * 1000 - raw_tick_ms) / 1000
    if not (-2 <= observed_age_s <= max_quote_age_sec):
        raise RuntimeError('QUOTE_STALE_OR_TIMEBASE_CHANGED: shadow capture blocked')
    return round(observed_age_s, 3)


def last_closed_raw_window(host_ms: int, offset_sec: int,
                           lag_ms: int = FINALITY_MS) -> tuple[int, int]:
    """Choose a *raw broker clock* minute, not an allegedly certified UTC minute."""
    end = ((host_ms + offset_sec * 1000 - lag_ms) // WINDOW_MS) * WINDOW_MS
    return end - WINDOW_MS, end


def filter_and_validate_ticks(ticks: np.ndarray, start_ms: int, end_ms: int) -> np.ndarray:
    if ticks is None:
        raise RuntimeError('MT5 history request failed; refuse empty success marker')
    if not isinstance(ticks, np.ndarray) or not ticks.dtype.names:
        raise RuntimeError('MT5 returned unstructured tick data')
    required = {'time_msc', 'bid', 'ask', 'time', 'flags'}
    if not required.issubset(ticks.dtype.names):
        raise RuntimeError(f'MT5 tick columns missing: {sorted(required-set(ticks.dtype.names))}')
    if len(ticks) > MAX_TICKS + 100:
        raise RuntimeError('Unbounded tick request: refuse oversized batch')
    # MT5 range upper bound may be inclusive. Only the half-open raw window is ours.
    rows = ticks[(ticks['time_msc'] >= start_ms) & (ticks['time_msc'] < end_ms)]
    if len(rows) > MAX_TICKS:
        raise RuntimeError('Tick window exceeds allowed size')
    if len(rows) == 0:
        return rows
    times = rows['time_msc'].astype('int64')
    if np.any(np.diff(times) < 0):
        raise RuntimeError('Out-of-order broker ticks: no silent sorting')
    bid = rows['bid'].astype('float64')
    ask = rows['ask'].astype('float64')
    if np.any(~np.isfinite(bid)) or np.any(~np.isfinite(ask)) or np.any(bid <= 0) or np.any(ask <= bid):
        raise RuntimeError('Invalid Bid/Ask: refusing shadow commit')
    # Whole-second timestamp is generally truncated; allow within one full second.
    seconds = rows['time'].astype('int64')
    if np.any(np.abs(times - seconds * 1000) > 1999):
        raise RuntimeError('Inconsistent time and time_msc')
    return rows


def prepare_shadow_table(rows: np.ndarray, *, observed_ms: int, request_start_ms: int,
                         batch_id: str, offset_sec: int, point: float,
                         observed_tick_minus_host_sec: float):
    """Lazily imports PyArrow. Broker raw time is int64, never timestamp[tz=UTC]."""
    import pyarrow as pa
    if len(rows) == 0 or not (math.isfinite(point) and point > 0):
        raise ValueError('Nonempty ticks and positive broker point required')
    n = len(rows)
    columns = {
        'raw_time_msc': pa.array(rows['time_msc'].astype('int64'), type=pa.int64()),
        'raw_time_seconds': pa.array(rows['time'].astype('int64'), type=pa.int64()),
        'bid': pa.array(rows['bid'].astype('float64'), type=pa.float64()),
        'ask': pa.array(rows['ask'].astype('float64'), type=pa.float64()),
        'flags': pa.array(rows['flags'].astype('int32'), type=pa.int32()),
        'spread_points': pa.array((rows['ask'].astype('float64') - rows['bid'].astype('float64')) / point,
                                  type=pa.float64()),
        'host_observed_utc_msc': pa.array(np.full(n, observed_ms, dtype=np.int64), type=pa.int64()),
        'host_request_start_utc_msc': pa.array(np.full(n, request_start_ms, dtype=np.int64), type=pa.int64()),
        'broker_offset_candidate_sec': pa.array(np.full(n, offset_sec, dtype=np.int32), type=pa.int32()),
        'observed_tick_minus_host_sec': pa.array(np.full(n, observed_tick_minus_host_sec, dtype='float64'), type=pa.float64()),
        'timebase_status': pa.array([STATUS] * n, type=pa.string()),
        'capture_batch_id': pa.array([batch_id] * n, type=pa.string()),
    }
    for name, kind in (('last', pa.float64()), ('volume', pa.uint64()), ('volume_real', pa.float64())):
        if name in rows.dtype.names:
            columns[name] = pa.array(rows[name], type=kind)
    return pa.table(columns).replace_schema_metadata({
        b'timebase': b'RAW_BROKER_UNVERIFIED',
        b'utc_time_certified': b'false',
        b'offset_certified': b'false',
        b'dst_certified': b'false',
        b'original_archive_immutable': b'true',
        b'schema_version': b'shadow-v1',
        b'host_observed_note': b'batch end/processing clock, not per-tick event time',
    })


def _sha(path: Path) -> str:
    hasher = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def _atomic_new_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with tmp.open('xb') as fd:
            fd.write(payload)
            fd.flush()
            os.fsync(fd.fileno())
        # Hardlink is atomic create-if-absent (unlike os.replace overwrite).
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_new_parquet(path: Path, table) -> str:
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        pq.write_table(table, tmp, compression='zstd', version='2.6')
        with tmp.open('rb+') as fd:
            os.fsync(fd.fileno())
        # Use ParquetFile, not read_table(path): a file under Hive-style directories
        # may otherwise gain synthetic partition columns during readback.
        readback = pq.ParquetFile(tmp).read()
        if readback.num_rows != table.num_rows or not readback.schema.equals(table.schema, check_metadata=True):
            raise RuntimeError('Parquet readback/schema mismatch')
        if not readback.column('raw_time_msc').equals(table.column('raw_time_msc')):
            raise RuntimeError('Parquet raw ticks differ after readback')
        digest = _sha(tmp)
        if path.exists():
            raise FileExistsError('Shadow file exists; immutable no-overwrite policy')
        os.link(tmp, path)
        return digest
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def exclusive_shadow_process(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / '.shadow_capture.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError('Shadow collector lock exists: inspect process before manual removal') from exc
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(f'pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat()}\n')
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


class ShadowCapture:
    def __init__(self, *, root: Path, mt5, server: str, login: int, offset_sec: int):
        if not login or not server or offset_sec % 900 != 0 or abs(offset_sec) > 14 * 3600:
            raise ValueError('Missing broker identity or invalid candidate offset')
        self.root, self.mt5, self.server, self.login, self.offset_sec = Path(root), mt5, server, int(login), int(offset_sec)
        self.identity_tag = sha256(f'{login}:{server}'.encode()).hexdigest()[:20]
        self._last_end_by_symbol: dict[str, int | None] = {}

    def _verify_account(self) -> None:
        account = self.mt5.account_info()
        if account is None or int(account.login) != self.login or account.server != self.server:
            raise RuntimeError('Connected MT5 account changed or disconnected')
        if account.trade_mode != self.mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError('Shadow capture DEMO account required')

    def _path(self, symbol: str, start: int, end: int) -> Path:
        # Date here is explicitly a RAW LABEL, never certified UTC.
        date_label = datetime.fromtimestamp(start / 1000, timezone.utc).strftime('%Y-%m-%d')
        return (self.root / 'parquet' / f'identity={self.identity_tag}' / f'symbol={safe_symbol(symbol)}'
                / f'raw_date={date_label}' / f'raw_{start}_{end}.parquet')

    def capture_once(self, symbol: str, *, host_ms: int | None = None) -> dict:
        self._verify_account()
        symbol = safe_symbol(symbol)
        if not self.mt5.symbol_select(symbol, True):
            raise RuntimeError('Symbol unavailable in MT5')
        info = self.mt5.symbol_info(symbol)
        if info is None or float(info.point) <= 0:
            raise RuntimeError('Invalid broker point value')
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None or int(getattr(tick, 'time_msc', 0)) <= 0:
            return {'symbol': symbol, 'state': 'NO_FRESH_QUOTE', 'archived': False}
        host_ms = int(time.time_ns() // 1_000_000) if host_ms is None else int(host_ms)
        try:
            age_s = checked_offset(int(tick.time_msc), host_ms, self.offset_sec)
        except RuntimeError:
            # Market closed/stale or broker timebase changed: no writes and no zero-tick marker.
            return {'symbol': symbol, 'state': 'STALE_OR_TIMEBASE_MISMATCH', 'archived': False}
        if (getattr(tick, 'time', 0) and abs(int(tick.time_msc) - int(tick.time) * 1000) > 1999):
            raise RuntimeError('Latest quote timestamp fields inconsistent')
        start, end = last_closed_raw_window(host_ms, self.offset_sec)
        dest = self._path(symbol, start, end)
        sidecar = dest.with_suffix('.manifest.json')
        # No automatic backfill or writing a previously indexed window.
        if dest.exists() or sidecar.exists():
            if not (dest.exists() and sidecar.exists()):
                raise RuntimeError('Orphan shadow archive or manifest: manual reconciliation required')
            manifest = json.loads(sidecar.read_text(encoding='utf-8'))
            if manifest.get('sha256') != _sha(dest):
                raise RuntimeError('Existing shadow archive checksum mismatch')
            return {'symbol': symbol, 'state': 'ALREADY_VERIFIED', 'archived': False,
                    'count': manifest.get('count', 0), 'raw_start': start}
        # Strict gap audit across raw-date partitions. A trading-session calendar is
        # NOT assumed: weekend/rollover/illiquid intervals are UNCLASSIFIED, not
        # blindly treated as collector faults or valid coverage.
        if symbol not in self._last_end_by_symbol:
            prefix = self.root / 'parquet' / f'identity={self.identity_tag}' / f'symbol={symbol}'
            prior = prefix.rglob('raw_*_*.manifest.json') if prefix.exists() else ()
            known = [int(p.name.split('_')[-1].split('.')[0]) for p in prior]
            self._last_end_by_symbol[symbol] = max(known) if known else None
        latest_end = self._last_end_by_symbol[symbol]
        if latest_end is not None:
            if latest_end > start:
                raise RuntimeError('Shadow window overlap/restart across older window; manual audit required')
            if latest_end < start:
                gap = (self.root / 'gaps' / f'identity={self.identity_tag}'
                       / f'symbol={symbol}' / f'gap_{latest_end}_{start}.json')
                if not gap.exists():
                    _atomic_new_bytes(gap, json.dumps({
                        'state':'UNCLASSIFIED_GAP_NOT_BACKFILLED', 'from_raw_ms':latest_end,
                        'to_raw_ms':start, 'timebase_status':STATUS,
                        'session_calendar_certified':False,
                        'note':'May be market close or missing data; must be classified offline'
                    }, sort_keys=True).encode('utf-8'))
        request_start = int(time.time_ns() // 1_000_000)
        # Query in the empirically observed raw broker-timestamp domain. UTC tzinfo
        # is an API transport format, NOT a claim that requested raw labels are UTC.
        from_dt = datetime.fromtimestamp(start / 1000, timezone.utc)
        to_dt = datetime.fromtimestamp(end / 1000, timezone.utc)
        ticks = self.mt5.copy_ticks_range(symbol, from_dt, to_dt, self.mt5.COPY_TICKS_ALL)
        response_end = int(time.time_ns() // 1_000_000)
        if ticks is None:
            raise RuntimeError('MT5 history query returned None; no archive record')
        # Freshness must remain within policy after MT5 call.
        newtick = self.mt5.symbol_info_tick(symbol)
        if newtick is None:
            raise RuntimeError('Broker quote disappeared during query')
        checked_offset(int(newtick.time_msc), response_end, self.offset_sec)
        self._verify_account()
        rows = filter_and_validate_ticks(ticks, start, end)
        if len(rows) == 0:
            marker = dest.with_suffix('.no_ticks_unverified.json')
            if marker.exists():
                return {'symbol':symbol, 'state':'NO_TICKS_UNVERIFIED_EXISTS','archived':False}
            _atomic_new_bytes(marker, json.dumps({'symbol':symbol,'state':'NO_TICKS_UNVERIFIED',
                'start_raw_ms':start,'end_raw_ms':end,'timebase_status':STATUS}, sort_keys=True).encode('utf-8'))
            return {'symbol':symbol,'state':'NO_TICKS_UNVERIFIED','archived':False}
        if dest.with_suffix('.no_ticks_unverified.json').exists():
            raise RuntimeError('Prior zero-tick marker exists: do not silently replace')
        batch_id = str(uuid.uuid4())
        latest_observed_delta = (int(newtick.time_msc) - response_end) / 1000
        table = prepare_shadow_table(rows, observed_ms=response_end, request_start_ms=request_start,
                                     batch_id=batch_id, offset_sec=self.offset_sec, point=float(info.point),
                                     observed_tick_minus_host_sec=latest_observed_delta)
        digest = _atomic_new_parquet(dest, table)
        # Sidecar is committed after Parquet creation; crash in-between produces orphan.
        manifest = {
            'schema_version':'shadow-v1', 'capture_batch_id':batch_id,
            'timebase_status':STATUS,'utc_time_certified':False,'dst_certified':False,
            'symbol':symbol,'identity_tag':self.identity_tag,
            'broker_offset_candidate_sec':self.offset_sec,'latest_quote_age_sec':age_s,
            'observed_tick_minus_host_sec':latest_observed_delta,
            'window_start_raw_ms':start,'window_end_raw_ms_exclusive':end,
            'host_request_start_utc_ms':request_start,'host_response_end_utc_ms':response_end,
            'count':int(len(rows)),'sha256':digest, 'bytes':dest.stat().st_size,
            'archive_eligible_for_training':False,'archive_eligible_for_execution':False,
        }
        _atomic_new_bytes(sidecar, json.dumps(manifest, indent=2, sort_keys=True).encode('utf-8'))
        self._last_end_by_symbol[symbol] = end
        return {'symbol':symbol, 'state':'SHADOW_RAW_CAPTURED_UNVERIFIED', 'archived':True,
                'count':int(len(rows)), 'raw_start':start, 'sha256':digest,
                'timebase_status':STATUS}


def capture_status(root: Path) -> dict:
    data_root = Path(root) / 'parquet'
    manifests = sorted(data_root.rglob('*.manifest.json'))
    parquet_files = set(data_root.rglob('*.parquet'))
    temporary_files = list(data_root.rglob('*.partial'))
    expected_files = {p.with_name(p.name.replace('.manifest.json', '.parquet')) for p in manifests}
    if parquet_files != expected_files or temporary_files:
        raise RuntimeError('Shadow orphan or partial files found: manual provenance audit required')
    reports = []
    for path in manifests:
        data = json.loads(path.read_text(encoding='utf-8'))
        parquet = path.with_name(path.name.replace('.manifest.json', '.parquet'))
        if not parquet.exists() or _sha(parquet) != data.get('sha256'):
            raise RuntimeError(f'Shadow archive integrity failure: {path.name}')
        reports.append(data)
    return {'archived_raw_windows':len(reports), 'rows':sum(x['count'] for x in reports),
            'raw_timebase_unverified':True, 'five_sessions_certified':False,
            'symbols':sorted(set(x['symbol'] for x in reports)),
            'raw_label_dates':sorted(set(datetime.fromtimestamp(x['window_start_raw_ms']/1000,timezone.utc).date().isoformat()
                                          for x in reports)),
            'note':'Days/rows do NOT certify five liquid sessions or UTC/DST; provenance review required.'}
