"""Read-only Phase 3.6 shadow session coverage and host-capture cadence audit.

Do not certify broker UTC/DST, broker hours, sessions, host sleep, or trading readiness.
All session labels use a *candidate* offset and IANA civic timezones, not certified
broker time. Shadow archive and original archive remain immutable.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import time

WINDOW_MS = 60_000
STATUS = 'UNVERIFIED_PENDING_DST'
MAX_SUSPECT_HOST_GAP_SEC = 130
MAX_SUSPECT_CAPTURE_LAG_SEC = 120
RECENT_INFLIGHT_SEC = 30


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _quantile(values: list[float | int], proportion: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    position = (len(data) - 1) * proportion
    lo = int(position)
    hi = min(lo + 1, len(data) - 1)
    return round(float(data[lo]) * (1 - (position - lo)) + float(data[hi]) * (position - lo), 3)


def _safe_dt(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(int(epoch_ms) / 1000, timezone.utc)


def _session_labels(candidate_utc_ms: int, zones: dict[str, ZoneInfo]) -> list[str]:
    """Illustrative standard business-hour labels, NOT a venue liquidity guarantee."""
    ts = _safe_dt(candidate_utc_ms)
    tokyo = ts.astimezone(zones['Asia/Tokyo'])
    london = ts.astimezone(zones['Europe/London'])
    newyork = ts.astimezone(zones['America/New_York'])
    asia = tokyo.weekday() < 5 and 9 <= tokyo.hour < 18
    uk = london.weekday() < 5 and 8 <= london.hour < 17
    ny = newyork.weekday() < 5 and 8 <= newyork.hour < 17
    result = []
    if asia:
        result.append('ASIA_TOKYO')
    if uk:
        result.append('LONDON')
    if ny:
        result.append('NEW_YORK')
    if uk and ny:
        result.append('LONDON_NY_OVERLAP')
    if not result:
        result.append('OTHER_HOURS')
    return result


def _session_local_date(candidate_utc_ms: int, label: str, zones: dict[str, ZoneInfo]) -> str:
    if label == 'OTHER_HOURS':
        return _safe_dt(candidate_utc_ms).date().isoformat()
    key = ('Asia/Tokyo' if label == 'ASIA_TOKYO' else
           'America/New_York' if label == 'NEW_YORK' else 'Europe/London')
    return _safe_dt(candidate_utc_ms).astimezone(zones[key]).date().isoformat()


def _rollover_candidate(candidate_utc_ms: int, zones: dict[str, ZoneInfo]) -> bool:
    ny = _safe_dt(candidate_utc_ms).astimezone(zones['America/New_York'])
    minutes = ny.hour * 60 + ny.minute
    return ny.weekday() < 5 and (16 * 60 + 55 <= minutes <= 17 * 60 + 15)


def _load_manifest(path: Path, parquet_path: Path, deep: bool) -> dict:
    d = json.loads(path.read_text(encoding='utf-8'))
    required = ('symbol', 'identity_tag', 'count', 'sha256', 'window_start_raw_ms',
                'window_end_raw_ms_exclusive', 'broker_offset_candidate_sec',
                'host_request_start_utc_ms', 'host_response_end_utc_ms', 'timebase_status')
    missing = set(required) - d.keys()
    if missing:
        raise ValueError(f'missing manifest fields: {sorted(missing)}')
    if d['timebase_status'] != STATUS or d.get('utc_time_certified') is not False:
        raise ValueError('unexpected or certified timebase status; do not trust')
    if not isinstance(d['symbol'], str) or not isinstance(d['identity_tag'], str):
        raise ValueError('invalid identity or symbol')
    start, end = int(d['window_start_raw_ms']), int(d['window_end_raw_ms_exclusive'])
    if end - start != WINDOW_MS or start % WINDOW_MS or int(d['count']) <= 0:
        raise ValueError('bad window bounds/count')
    if int(d['host_response_end_utc_ms']) < int(d['host_request_start_utc_ms']):
        raise ValueError('host response before request start')
    if (f'identity={d["identity_tag"]}' not in parquet_path.parts or
            f'symbol={d["symbol"]}' not in parquet_path.parts or
            parquet_path.name != f'raw_{start}_{end}.parquet'):
        raise ValueError('manifest does not match archive location')
    if not parquet_path.is_file() or _sha256(parquet_path) != d['sha256']:
        raise ValueError('missing or hash-mismatched parquet')
    if deep:
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(parquet_path)
        if pf.metadata.num_rows != int(d['count']):
            raise ValueError('Parquet row count disagrees with manifest')
        md = pf.schema_arrow.metadata or {}
        if md.get(b'timebase') != b'RAW_BROKER_UNVERIFIED' or md.get(b'utc_time_certified') != b'false':
            raise ValueError('Parquet timebase metadata mismatch')
        # Load ONLY raw timestamp to check actual window bounds and ordering.
        times = pf.read(columns=['raw_time_msc']).column('raw_time_msc').to_numpy()
        import numpy as np
        if len(times) != int(d['count']) or np.any(np.diff(times) < 0) or np.any(times < start) or np.any(times >= end):
            raise ValueError('Parquet raw tick time ordering/bounds incorrect')
    return d


def audit(root: Path, *, deep: bool = False) -> dict:
    """Audit immutable shadow artifacts without opening MT5 or modifying anything."""
    root = Path(root)
    data_dir = root / 'parquet'
    manifests = sorted(data_dir.rglob('*.manifest.json')) if data_dir.exists() else []
    parquets = set(data_dir.rglob('*.parquet')) if data_dir.exists() else set()
    expected = {p.with_name(p.name.replace('.manifest.json', '.parquet')) for p in manifests}
    now = time.time()
    errors, warnings = [], []
    for orphan in sorted(parquets - expected):
        age = now - orphan.stat().st_mtime
        (warnings if age < RECENT_INFLIGHT_SEC else errors).append(
            {'code': 'INFLIGHT_RECENT_ORPHAN' if age < RECENT_INFLIGHT_SEC else 'ORPHAN_PARQUET', 'file': str(orphan.relative_to(root))})
    for partial in (data_dir.rglob('*.partial') if data_dir.exists() else []):
        age = now - partial.stat().st_mtime
        (warnings if age < RECENT_INFLIGHT_SEC else errors).append(
            {'code': 'RECENT_PARTIAL' if age < RECENT_INFLIGHT_SEC else 'STALE_PARTIAL', 'file': str(partial.relative_to(root))})
    records = []
    for manifest in manifests:
        parquet = manifest.with_name(manifest.name.replace('.manifest.json', '.parquet'))
        try:
            d = _load_manifest(manifest, parquet, deep)
            d['_file_bytes'] = parquet.stat().st_size
            d['_manifest_bytes'] = manifest.stat().st_size
            records.append(d)
        except Exception as exc:  # Include Parquet's ArrowInvalid; fail closed and report the bad file.
            errors.append({'code': 'INVALID_MANIFEST_OR_PARQUET', 'file': str(manifest.relative_to(root)),
                           'reason': str(exc)[:160]})
    zones = {}
    try:
        for k in ('Asia/Tokyo', 'Europe/London', 'America/New_York'):
            zones[k] = ZoneInfo(k)
    except ZoneInfoNotFoundError:
        warnings.append({'code': 'IANA_TZDATA_MISSING', 'remedy': 'Install tzdata; no session classification available'})
        zones.clear()
    grouped = defaultdict(list)
    for d in records:
        grouped[(d['identity_tag'], d['symbol'])].append(d)
    groups = []
    duplicate_window_count = 0
    for (identity, symbol), data in sorted(grouped.items()):
        data.sort(key=lambda d: int(d['window_start_raw_ms']))
        starts = [int(d['window_start_raw_ms']) for d in data]
        duplicate_window_count += len(starts) - len(set(starts))
        offsets = {int(d['broker_offset_candidate_sec']) for d in data}
        offset_consistent = len(offsets) == 1
        session_counter = Counter()
        session_dates = defaultdict(set)
        raw_dates = set()
        gap_list = []
        host_gap_list = []
        lag_list = []
        rollover_count = 0
        # Per-file loops are intentional (metadata audit); tick-level calculations are vectorized in --deep.
        for i, d in enumerate(data):
            start, end = int(d['window_start_raw_ms']), int(d['window_end_raw_ms_exclusive'])
            raw_dates.add(_safe_dt(start).date().isoformat())  # Raw label date, NOT UTC-certified.
            candidate = start - int(d['broker_offset_candidate_sec']) * 1000
            if zones and offset_consistent:
                labels = _session_labels(candidate, zones)
                for label in labels:
                    session_counter[label] += 1
                    session_dates[label].add(_session_local_date(candidate, label, zones))
                if _rollover_candidate(candidate, zones):
                    rollover_count += 1
            else:
                session_counter['UNCLASSIFIED_TIMEBASE'] += 1
            lag_seconds = (int(d['host_response_end_utc_ms']) + int(d['broker_offset_candidate_sec']) * 1000 - end) / 1000
            lag_list.append(round(lag_seconds, 3))
            if i:
                prev = data[i-1]
                last_end = int(prev['window_end_raw_ms_exclusive'])
                missing_mins = (start - last_end) // WINDOW_MS
                if missing_mins < 0:
                    errors.append({'code': 'OVERLAPPING_WINDOWS', 'symbol': symbol, 'start_raw_ms': start})
                elif missing_mins > 0:
                    gap_candidate_ms = last_end - int(d['broker_offset_candidate_sec']) * 1000
                    near_rollover = bool(zones and offset_consistent and (
                        _rollover_candidate(gap_candidate_ms, zones) or _rollover_candidate(candidate, zones)))
                    gap_list.append({'from_raw_ms': last_end, 'to_raw_ms': start,
                                     'missing_minutes': missing_mins,
                                     'classification': 'UNCLASSIFIED_MARKET_OR_COLLECTOR',
                                     'near_ny_rollover_candidate': near_rollover})
                observed_gap = (int(d['host_response_end_utc_ms']) - int(prev['host_response_end_utc_ms'])) / 1000
                if observed_gap > MAX_SUSPECT_HOST_GAP_SEC:
                    host_gap_list.append({'from_raw_ms': last_end, 'to_raw_ms': start,
                                          'host_observation_gap_seconds': round(observed_gap, 3),
                                          'state': 'POSSIBLE_CAPTURE_PAUSE_OR_MARKET_CLOSE_NOT_PROVEN_SLEEP'})
        sizes = [int(x['_file_bytes']) for x in data]
        lag_bad = sum(x > MAX_SUSPECT_CAPTURE_LAG_SEC or x < -15 for x in lag_list)
        if len(offsets) > 1:
            warnings.append({'code': 'OFFSET_CHANGED_WITHIN_GROUP', 'symbol': symbol, 'offsets_sec': sorted(offsets)})
        if sizes and median(sizes) < 32 * 1024:
            warnings.append({'code': 'MANY_SMALL_PARQUET_FILES', 'symbol': symbol,
                             'median_bytes': round(median(sizes)),
                             'note': 'Monitor growth; immutable source must not be deleted or compacted in place'})
        groups.append({
            'symbol': symbol, 'identity_tag': identity,
            'raw_label_dates_not_certified_utc': sorted(raw_dates),
            'recorded_windows': len(data), 'recorded_quote_rows': sum(int(x['count']) for x in data),
            'observed_raw_span_minutes': (int(data[-1]['window_end_raw_ms_exclusive']) - starts[0]) // WINDOW_MS,
            'candidate_session_minutes_not_certified': dict(sorted(session_counter.items())),
            'candidate_session_local_dates_not_certified': {k: sorted(v) for k, v in sorted(session_dates.items())},
            'offset_candidates_seconds': sorted(offsets),
            'missing_window_minutes_unclassified': sum(g['missing_minutes'] for g in gap_list),
            'gap_count': len(gap_list), 'gaps_first_25': gap_list[:25],
            'host_observation_gaps_over_130s_count': len(host_gap_list),
            'host_observation_gaps_first_25': host_gap_list[:25],
            'capture_lag_seconds_p95': _quantile(lag_list, .95),
            'capture_lag_suspicious_count': lag_bad,
            'rollover_candidate_windows': rollover_count,
            'file_size_bytes_p50': _quantile(sizes, .5),
            'file_size_bytes_p95': _quantile(sizes, .95),
        })
    if duplicate_window_count:
        errors.append({'code': 'DUPLICATE_WINDOWS', 'count': duplicate_window_count})
    if len(records) >= 2000:
        warnings.append({'code': 'HIGH_SMALL_FILE_COUNT', 'files': len(records),
                         'note': 'Consider a separate, source-hashed hourly derived view; never rewrite raw shadow files'})
    all_sizes = [d['_file_bytes'] for d in records]
    return {
        'tool': 'Phase 3.6 Read-only Shadow Session Coverage Audit',
        'read_only': True, 'sends_orders': False, 'writes_archive': False,
        'archive_timebase': STATUS, 'utc_dst_certified': False,
        'five_liquid_sessions_certified': False, 'live_capture_monitoring': False,
        'assessment': 'AUDIT_FAILED' if errors else ('PROVISIONAL_COVERAGE_ONLY' if records else 'NO_DATA'),
        'manifest_count': len(manifests), 'validated_window_count': len(records),
        'total_recorded_quote_ticks': sum(int(x['count']) for x in records),
        'total_parquet_bytes': sum(all_sizes), 'total_manifest_bytes': sum(d['_manifest_bytes'] for d in records),
        'global_file_size_bytes_p50': _quantile(all_sizes, .5),
        'global_file_size_bytes_p95': _quantile(all_sizes, .95),
        'candidate_offset_notice': 'Civic session labels require broker UTC mapping; these are provisional hypotheses',
        'session_hours_used': 'Tokyo 09-18 local; London 08-17 local; New York 08-17 local; overlap can be double-labelled',
        'rollover_notice': '17:00 New York is a diagnostic candidate only; actual MetaQuotes-Demo instrument schedule is unverified',
        'gap_notice': 'Missing archive minute cannot by itself distinguish market closure, lack of quotes, crash, sleep or network outage',
        'host_notice': 'File-side host observation gaps are not a continuous heartbeat. Dedicated heartbeat+monotonic telemetry required for sleep confirmation',
        'small_files_policy': 'No compaction of immutable raw files; derive versioned hourly data later with source hashes and retention',
        'groups': groups, 'warnings': warnings[:150], 'errors': errors[:150],
        'truncated_warning_or_error_lists': len(warnings) > 150 or len(errors) > 150,
    }
