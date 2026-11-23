"""Phase 3.6.5 — Read-only provisional session / capture-continuity evidence.

NO MT5/Ollama/Discord/requests/trading, NO archive edits, NO timebase certification.
Correlates heartbeat with HOST query response in Shadow manifest, never with
raw broker time. A healthy process snapshot is NOT evidence of MT5 connectivity,
no missed ticks, or market being open. Does not certify 5 sessions.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from mt5_agents.phase36_session_coverage import audit  # existing hash/row audit
from phase364_window_attestation import (  # existing vetted pulse parser and semantics
    _heartbeat_paths, _read_heartbeat_jsonl, _read_json_file, _healthy_pulse,
    _in_tree,
)

SCHEMA = 'phase365-provisional-continuity-v1'
RAW_TIMEBASE = 'UNVERIFIED_PENDING_DST'
BEFORE_MS = 30_000
AFTER_MS = 30_000
MAX_GAP_MS = 25_000
MAX_MANIFESTS = 300_000
MAX_PULSES = 250_000


def load_verified_manifests(project: Path, coverage: dict) -> list[dict]:
    """Require existing coverage audit to succeed BEFORE consuming its manifests."""
    if coverage.get('assessment') == 'AUDIT_FAILED' or coverage.get('errors'):
        raise ValueError('SOURCE_AUDIT_FAILED_NO_CONTINUITY_CERTIFICATION')
    archive = project / 'runtime' / 'shadow_tick_archive' / 'parquet'
    if archive.is_symlink():
        raise ValueError('ARCHIVE_DIRECTORY_SYMLINK_FORBIDDEN')
    files = sorted(archive.rglob('*.manifest.json')) if archive.exists() else []
    if len(files) > MAX_MANIFESTS:
        raise ValueError('TOO_MANY_MANIFESTS_FOR_SINGLE_AUDIT')
    if len(files) != coverage.get('validated_window_count') or len(files) != coverage.get('manifest_count'):
        raise ValueError('ARCHIVE_CHANGED_DURING_AUDIT_RETRY')
    rows = []
    seen = set()
    for path in files:
        _in_tree(path, archive)
        d = _read_json_file(path)
        if d.get('timebase_status') != RAW_TIMEBASE or d.get('utc_time_certified') is not False:
            raise ValueError('INCONSISTENT_TIMEBASE_MANIFEST')
        req, end = d.get('host_request_start_utc_ms'), d.get('host_response_end_utc_ms')
        if type(req) is not int or type(end) is not int or req <= 0 or not req <= end <= req + 300_000:
            raise ValueError('HOST_RESPONSE_INVALID')
        if type(d.get('window_start_raw_ms')) is not int or type(d.get('window_end_raw_ms_exclusive')) is not int:
            raise ValueError('RAW_WINDOW_BOUNDS_INVALID')
        if d['window_end_raw_ms_exclusive'] - d['window_start_raw_ms'] != 60_000:
            raise ValueError('RAW_WINDOW_DURATION_INVALID')
        identity = (d.get('identity_tag'), d.get('symbol'), d['window_start_raw_ms'])
        if identity in seen:
            raise ValueError('DUPLICATE_IDENTITY_SYMBOL_RAW_WINDOW')
        seen.add(identity)
        rows.append({
            'identity_tag': d['identity_tag'], 'symbol': d['symbol'],
            'raw_start_ms_not_certified_utc': d['window_start_raw_ms'],
            'host_response_end_utc_ms': end, 'capture_batch_id': d.get('capture_batch_id'),
        })
    return rows


def load_heartbeat_once(project: Path, rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    earliest = min(x['host_response_end_utc_ms'] for x in rows) - BEFORE_MS
    latest = max(x['host_response_end_utc_ms'] for x in rows) + 180_000
    telemetry_dir = project / 'runtime' / 'telemetry'
    paths = _heartbeat_paths(project, earliest, latest)
    distinct = {}
    for path in paths:
        _in_tree(path, telemetry_dir)
        for rec in _read_heartbeat_jsonl(path):
            ms = rec['host_utc_msc']
            if ms < earliest or ms > latest:
                continue
            key = (ms, rec['monotonic_ns'])
            if key in distinct and distinct[key] != rec:
                raise ValueError('DUPLICATE_HEARTBEAT_CONFLICT')
            distinct[key] = rec
            if len(distinct) > MAX_PULSES:
                raise ValueError('TOO_MANY_HEARTBEATS_FOR_SINGLE_AUDIT')
    return sorted(distinct.values(), key=lambda x: x['host_utc_msc'])


def evaluate_pulse_bracket(host_ms: int, pulses: list[dict], host_times: list[int] | None = None) -> dict:
    """Classify process observations near host response, NOT true market coverage."""
    stamps = host_times if host_times is not None else [h['host_utc_msc'] for h in pulses]
    idx_pre = bisect_right(stamps, host_ms) - 1
    idx_post = bisect_left(stamps, host_ms)
    pre = pulses[idx_pre] if idx_pre >= 0 else None
    post = pulses[idx_post] if idx_post < len(pulses) else None
    base = {
        'pulse_before_host_utc_ms': pre['host_utc_msc'] if pre else None,
        'pulse_after_host_utc_ms': post['host_utc_msc'] if post else None,
        'max_inter_pulse_seconds': None,
    }
    if (pre is None or post is None or
        host_ms - pre['host_utc_msc'] > BEFORE_MS or
        post['host_utc_msc'] - host_ms > AFTER_MS):
        return dict(base, process_evidence='UNVERIFIED_MISSING_BRACKET')
    # Include every pulse from nearest pre to nearest post; tie on exact timestamp
    # requires a distinct subsequent observation (one pulse is not a cadence).
    if idx_post <= idx_pre:
        idx_post = idx_pre + 1
        if idx_post >= len(pulses) or pulses[idx_post]['host_utc_msc'] - host_ms > AFTER_MS:
            return dict(base, process_evidence='UNVERIFIED_INSUFFICIENT_CADENCE')
        post = pulses[idx_post]
        base['pulse_after_host_utc_ms'] = post['host_utc_msc']
    span = pulses[idx_pre:idx_post+1]
    if len(span) < 2:
        return dict(base, process_evidence='UNVERIFIED_INSUFFICIENT_CADENCE')
    gaps = []
    for a, b in zip(span, span[1:]):
        wall_delta = b['host_utc_msc'] - a['host_utc_msc']
        mono_delta_ms = (b['monotonic_ns'] - a['monotonic_ns']) / 1_000_000
        if wall_delta <= 0 or mono_delta_ms <= 0 or abs(wall_delta - mono_delta_ms) > 3000:
            return dict(base, process_evidence='CLOCK_OR_SEQUENCE_UNCERTAIN')
        gaps.append(wall_delta)
    base['max_inter_pulse_seconds'] = round(max(gaps) / 1000, 3)
    if any(r['wall_clock_shift_suspected_bool'] for r in span):
        return dict(base, process_evidence='CLOCK_OR_SEQUENCE_UNCERTAIN')
    if max(gaps) > MAX_GAP_MS or any(r['watchdog_interval_overrun_bool'] for r in span):
        return dict(base, process_evidence='WATCHDOG_CADENCE_GAP_CAUSE_UNKNOWN')
    if not all(_healthy_pulse(r) for r in span):
        return dict(base, process_evidence='COLLECTOR_OR_WATCHDOG_UNVERIFIED_OR_ALERT')
    return dict(base, process_evidence='WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE')


def build_matrix(project: Path, *, deep: bool = False, include_windows: bool = False,
                 coverage_report: dict | None = None, manifest_rows: list[dict] | None = None,
                 heartbeat_rows: list[dict] | None = None) -> dict:
    """Dependencies injectable for unit tests; real CLI always runs validated audit."""
    coverage = coverage_report if coverage_report is not None else audit(project / 'runtime' / 'shadow_tick_archive', deep=deep)
    rows = manifest_rows if manifest_rows is not None else load_verified_manifests(project, coverage)
    pulses = heartbeat_rows if heartbeat_rows is not None else load_heartbeat_once(project, rows)
    stamps = [h['host_utc_msc'] for h in pulses]
    if stamps != sorted(stamps):
        raise ValueError('PULSES_NOT_SORTED')
    if coverage.get('errors') or coverage.get('assessment') == 'AUDIT_FAILED':
        raise ValueError('COVERAGE_AUDIT_FAILED')
    if len(rows) != coverage.get('validated_window_count'):
        raise ValueError('COVERAGE_MANIFEST_ROW_COUNT_MISMATCH')
    counts = Counter()
    per_symbol: dict[tuple[str, str], Counter] = defaultdict(Counter)
    detail = []
    for r in sorted(rows, key=lambda x: (x['symbol'], x['raw_start_ms_not_certified_utc'])):
        m = evaluate_pulse_bracket(r['host_response_end_utc_ms'], pulses, stamps)
        status = m['process_evidence']
        counts[status] += 1
        per_symbol[(r['identity_tag'], r['symbol'])][status] += 1
        # Snapshot 'last_saved_batch_id' is not an event log for all capture batches.
        # Its absence is NOT an archive error, only lack of per-batch observation.
        near = [h for h in pulses[bisect_left(stamps, r['host_response_end_utc_ms']):
                                bisect_right(stamps, r['host_response_end_utc_ms'] + 180_000)]
                if h.get('last_saved_batch_id') == r['capture_batch_id']]
        if near:
            counts['BATCH_ID_SEEN'] += 1
            per_symbol[(r['identity_tag'], r['symbol'])]['BATCH_ID_SEEN'] += 1
        if include_windows or status != 'WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE':
            detail.append({
                'symbol': r['symbol'], 'identity_tag': r['identity_tag'],
                'raw_start_ms_not_certified_utc': r['raw_start_ms_not_certified_utc'],
                'host_response_end_utc_ms': r['host_response_end_utc_ms'],
                'capture_batch_id_seen_in_watchdog': bool(near), **m,
            })
    groups = []
    for g in coverage['groups']:
        key = (g['identity_tag'], g['symbol'])
        c = per_symbol[key]
        groups.append({
            'symbol': g['symbol'], 'identity_tag': g['identity_tag'],
            'validated_raw_windows': g['recorded_windows'],
            'recorded_quote_ticks': g['recorded_quote_rows'],
            'observed_raw_span_minutes': g['observed_raw_span_minutes'],
            'archive_missing_window_minutes_unclassified': g['missing_window_minutes_unclassified'],
            'archive_host_observation_gaps_over_130s': g['host_observation_gaps_over_130s_count'],
            'candidate_session_minutes_not_certified': g['candidate_session_minutes_not_certified'],
            'candidate_session_local_dates_not_certified': g['candidate_session_local_dates_not_certified'],
            'watchdog_observed_windows': c['WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE'],
            'watchdog_unverified_windows': g['recorded_windows'] - c['WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE'],
            'capture_batch_id_seen_in_watchdog_windows': c['BATCH_ID_SEEN'],
            'continuity_detail_counts': {k: v for k, v in sorted(c.items()) if k != 'BATCH_ID_SEEN'},
        })
    observed = counts['WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE']
    return {
        'schema_version': SCHEMA,
        'status': ('PROVISIONAL_SOURCE_AND_PROCESS_EVIDENCE' if rows and observed == len(rows) else
                   'PROVISIONAL_SOURCE_EVIDENCE_PARTIAL_MONITORING' if rows else 'NO_WINDOWS'),
        'read_only': True, 'no_orders': True, 'no_mt5_calls': True,
        'no_discord_or_ollama_calls': True, 'archive_modified': False,
        'timebase_status': RAW_TIMEBASE, 'utc_dst_certified': False,
        'five_sessions_certified': False, 'baseline_ready': False,
        'liquid_session_labels': 'PROVISIONAL_CANDIDATES_NOT_VERIFIED',
        'observation_does_not_prove': [
            'MT5 connectivity', 'all broker quote ticks captured', 'venue session open',
            'Windows sleep cause', 'broker UTC/DST mapping', 'trading readiness',
        ],
        'coverage_audit': {
            'assessment': coverage['assessment'], 'deep_parquet_row_audit': deep,
            'validated_window_count': coverage['validated_window_count'],
            'total_recorded_quote_ticks': coverage['total_recorded_quote_ticks'],
            'manifest_count': coverage['manifest_count'],
            'warnings': coverage['warnings'], 'errors': coverage['errors'],
        },
        'heartbeat': {
            'valid_pulses_loaded_once': len(pulses),
            'monitored_window_count': observed,
            'unverified_window_count': len(rows) - observed,
            'batch_id_seen_window_count': counts['BATCH_ID_SEEN'],
            'classification_counts': {k: v for k, v in sorted(counts.items()) if k != 'BATCH_ID_SEEN'},
            'correlation_target': 'MANIFEST_HOST_RESPONSE_UTC_MS_NOT_BROKER_RAW_OR_PARQUET_COMMIT',
        },
        'groups': groups,
        'exception_or_window_details': detail if include_windows else detail[:25],
        'details_truncated': not include_windows and len(detail) > 25,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only Phase 3.6.5 provisional coverage + heartbeat evidence')
    parser.add_argument('--project-root', type=Path, default=ROOT)
    parser.add_argument('--deep', action='store_true', help='Hash and inspect all Parquet rows via existing session audit')
    parser.add_argument('--include-windows', action='store_true', help='Print all window classifications, not just exceptions')
    args = parser.parse_args(argv)
    try:
        # Live capture can atomically publish a NEW minute between the source
        # audit and our manifest inventory. Retry only this benign snapshot race.
        for attempt in range(2):
            try:
                out = build_matrix(args.project_root.resolve(), deep=args.deep, include_windows=args.include_windows)
                break
            except ValueError as exc:
                if 'ARCHIVE_CHANGED_DURING_AUDIT_RETRY' not in str(exc) or attempt:
                    raise
        print(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if out['status'] != 'PROVISIONAL_SOURCE_EVIDENCE_PARTIAL_MONITORING' else 3
    except (ValueError, RuntimeError, ImportError, OSError, KeyError, TypeError, AssertionError) as exc:
        print('PHASE365_FAIL_CLOSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
