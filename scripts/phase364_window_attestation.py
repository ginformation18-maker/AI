"""Phase 3.6.4: immutable Shadow window provenance and out-of-band heartbeat correlation.

Read-only by default. No MT5, Discord, Ollama, order submission, or modification of
source Parquet, Shadow manifests, derived JSON, or heartbeat logs. Optional
--write-deep-certificate adds ONLY a new immutable research certificate under
runtime/window_attestations; requires a successful deterministic recomputation.

IMPORTANT: raw_time_msc is NOT certified UTC. Correlate heartbeat ONLY with
manifest host_response_end_utc_ms (MT5 query response end, NOT file commit time).
Heartbeat process evidence does NOT certify MT5 connectivity or quote completeness.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from typing import Any

SYMBOLS = ('XAUUSD', 'EURUSD')
RAW_STATUS = 'UNVERIFIED_PENDING_DST'
CERT_SCHEMA = 'phase364-deep-certificate-v1'
MAX_JSON_BYTES = 1_048_576
MAX_HEARTBEAT_FILE_BYTES = 32 * 1024 * 1024
MAX_WINDOWS_PER_SYMBOL = 30
MAX_SOURCE_CANDIDATES = 5


def _module(path: Path, name: str):
    if not path.is_file():
        raise ValueError(f'REQUIRED_MODULE_MISSING: {path}')
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f'CANNOT_IMPORT_MODULE: {path}')
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'DUPLICATE_JSON_KEY: {key}')
        result[key] = value
    return result


def _no_constant(x):
    raise ValueError(f'NONFINITE_JSON_VALUE: {x}')


def _parse_json_bytes(raw: bytes):
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_keys, parse_constant=_no_constant)


def _read_json_file(path: Path, max_bytes: int = MAX_JSON_BYTES):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'FILE_MISSING_OR_SYMLINK: {path}')
    if path.stat().st_size > max_bytes:
        raise ValueError(f'FILE_TOO_LARGE: {path}')
    # A manifest/derived JSON is only read after it has been atomically published.
    # A short retry handles a transient Windows sharing violation.
    for attempt in range(2):
        try:
            payload = path.read_bytes()
            return _parse_json_bytes(payload)
        except PermissionError:
            if attempt:
                raise
            time.sleep(.05)
    raise AssertionError('unreachable')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for attempt in range(2):
        try:
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            return digest.hexdigest()
        except PermissionError:
            if attempt:
                raise
            digest = hashlib.sha256()
            time.sleep(.05)
    raise AssertionError('unreachable')


def _in_tree(path: Path, base: Path):
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f'PATH_OUTSIDE_EXPECTED_ROOT: {path}')
    cur = path
    while cur != base and cur != cur.parent:
        if cur.is_symlink():
            raise ValueError(f'SYMLINK_NOT_ALLOWED: {cur}')
        cur = cur.parent


def contexts_to_check(root: Path, symbol: str, recent: int, guard) -> list[tuple[dict, Path]]:
    folder = root / 'runtime' / 'quant_context_derived' / 'phase363-v2' / symbol
    if folder.is_symlink():
        raise ValueError('DERIVED_DIRECTORY_SYMLINK_FORBIDDEN')
    paths = list(folder.glob('raw-*.json'))
    if not paths:
        raise ValueError(f'NO_SAVED_DERIVED_JSON_FOR_{symbol}: run phase363_polars_engine.py --symbol {symbol}')
    rows = []
    for path in paths:
        _in_tree(path, folder)
        context = _read_json_file(path)
        guard.validate_context(context)
        if context['symbol'] != symbol or path.name != context['window_id'] + '.json':
            raise ValueError('DERIVED_PATH_SYMBOL_OR_WINDOW_MISMATCH')
        rows.append((context, path))
    rows.sort(key=lambda x: (x[0]['raw_start_ms'], x[0]['window_id']), reverse=True)
    starts = [x[0]['raw_start_ms'] for x in rows]
    if len(starts) != len(set(starts)):
        raise ValueError(f'AMBIGUOUS_DERIVED_WINDOWS_{symbol}')
    return rows[:recent]


def _window_id(symbol: str, identity_tag: str, start: int, end: int, parquet_sha: str) -> str:
    payload = f'{symbol}|{identity_tag}|{start}|{end}|{parquet_sha}'.encode('ascii')
    return 'raw-' + hashlib.sha256(payload).hexdigest()[:16]


def verify_provenance(root: Path, context: dict, engine) -> tuple[dict, Path, Path, dict]:
    """Verify source SHA, manifest, broker identity, capture batch and window ID.

    The SHA alone does NOT validate the numerical contents of the derived JSON.
    """
    symbol = context['symbol']
    start, end = context['raw_start_ms'], context['raw_end_exclusive_ms']
    archive = root / 'runtime' / 'shadow_tick_archive' / 'parquet'
    match_glob = f'identity=*/symbol={symbol}/raw_date=*/raw_{start}_{end}.manifest.json'
    files = list(archive.glob(match_glob))
    if not files or len(files) > MAX_SOURCE_CANDIDATES:
        raise ValueError(f'SHADOW_MANIFEST_MISSING_OR_AMBIGUOUS: {symbol} count={len(files)}')
    verified = []
    for mp in files:
        _in_tree(mp, archive)
        manifest = _read_json_file(mp)
        if manifest.get('sha256') != context['source']['sha256']:
            continue
        parquet = mp.with_name(mp.name.removesuffix('.manifest.json') + '.parquet')
        _in_tree(parquet, archive)
        engine.verify_manifest(parquet, manifest)  # hashes exact bytes, checks path/count/schema
        if manifest['symbol'] != symbol or manifest['window_start_raw_ms'] != start or manifest['window_end_raw_ms_exclusive'] != end:
            raise ValueError('MANIFEST_WINDOW_MISMATCH')
        bid = manifest['capture_batch_id']
        try:
            parsed_id = uuid.UUID(bid)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError('INVALID_CAPTURE_BATCH_UUID') from exc
        if parsed_id.version != 4 or str(parsed_id) != bid:
            raise ValueError('INVALID_CAPTURE_BATCH_UUID_VERSION')
        req = manifest.get('host_request_start_utc_ms')
        res = manifest.get('host_response_end_utc_ms')
        if type(req) is not int or type(res) is not int or req <= 0 or not req <= res <= req + 300_000:
            raise ValueError('INVALID_HOST_QUERY_TIME_RANGE')
        if any(manifest.get(k) is not False for k in ('utc_time_certified', 'dst_certified', 'archive_eligible_for_training', 'archive_eligible_for_execution')):
            raise ValueError('RAW_ARCHIVE_MUST_REMAIN_UNCERTIFIED_AND_RESEARCH_ONLY')
        if context['source']['kind'] != 'SHADOW_RAW_PARQUET' or context['source']['collector_schema'] != 'shadow-v1':
            raise ValueError('INVALID_SOURCE_KIND_OR_COLLECTOR_SCHEMA')
        if context['timebase_status'] != RAW_STATUS or context['utc_start_ms'] is not None:
            raise ValueError('RAW_TIMEBASE_MUST_REMAIN_UNVERIFIED')
        expected_window = _window_id(symbol, manifest['identity_tag'], start, end, manifest['sha256'])
        if context['window_id'] != expected_window:
            raise ValueError('WINDOW_ID_DOES_NOT_MATCH_SOURCE_PROVENANCE')
        verified.append((manifest, parquet, mp))
    if len(verified) != 1:
        raise ValueError(f'SHADOW_SOURCE_IDENTITY_NOT_UNIQUE_OR_MISSING: count={len(verified)}')
    m, raw, mp = verified[0]
    evidence = {
        'window_id': context['window_id'], 'symbol': symbol,
        'identity_tag': m['identity_tag'], 'capture_batch_id': m['capture_batch_id'],
        'raw_start_ms_not_certified_utc': start,
        'raw_end_exclusive_ms_not_certified_utc': end,
        'host_request_start_utc_ms': m['host_request_start_utc_ms'],
        'host_response_end_utc_ms': m['host_response_end_utc_ms'],
        'raw_parquet_sha256': m['sha256'], 'manifest_sha256': _sha256(mp),
        'raw_row_count_declared': m['count'],
        'provenance_status': 'SHA_IDENTITY_BATCH_WINDOW_VERIFIED',
    }
    return m, raw, mp, evidence


def _expected_certificate(root: Path, context: dict, derived: Path, evidence: dict) -> dict:
    return {
        'schema_version': CERT_SCHEMA,
        'verification_method': 'EXACT_RECOMPUTED_POLARS_CONTEXT_EQUALITY',
        'symbol': context['symbol'], 'window_id': context['window_id'],
        'identity_tag': evidence['identity_tag'],
        'capture_batch_id': evidence['capture_batch_id'],
        'raw_parquet_sha256': evidence['raw_parquet_sha256'],
        'manifest_sha256': evidence['manifest_sha256'],
        'derived_json_sha256': _sha256(derived),
        'engine_py_sha256': _sha256(root / 'scripts' / 'phase363_polars_engine.py'),
        'strict_guard_py_sha256': _sha256(root / 'phase363_contract_v2' / 'strict_guard.py'),
        'timebase_status': RAW_STATUS, 'research_only': True, 'no_orders': True,
    }


def _certificate_path(root: Path, context: dict) -> Path:
    return (root / 'runtime' / 'window_attestations' / 'phase364-v1' / context['symbol'] /
            f'{context["window_id"]}.deep.json')


def _save_immutable_certificate(path: Path, value: dict) -> None:
    """Create only; NO overwrite. File is a local test cache, not a signed signature."""
    data = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError('DEEP_CERTIFICATE_CONFLICT_NO_OVERWRITE')
        return
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with temp.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError('DEEP_CERTIFICATE_CONFLICT_NO_OVERWRITE')
    finally:
        temp.unlink(missing_ok=True)


def verify_metric_evidence(root: Path, context: dict, derived: Path, evidence: dict,
                           manifest: dict, parquet: Path, engine, mode: str,
                           write_deep_certificate: bool) -> tuple[str, str | None]:
    expected = _expected_certificate(root, context, derived, evidence)
    cert_path = _certificate_path(root, context)
    if mode == 'deep':
        recomputed = engine.calculate_metrics(parquet, manifest)
        if recomputed != context:
            raise ValueError('DERIVED_JSON_MISMATCH_RECOMPUTED_POLARS')
        if write_deep_certificate:
            _save_immutable_certificate(cert_path, expected)
        return 'RECOMPUTED_EXACT_MATCH', (str(cert_path) if write_deep_certificate else None)
    if mode == 'fast':
        if write_deep_certificate:
            raise ValueError('FAST_MODE_CANNOT_CREATE_DEEP_CERTIFICATE')
        if not cert_path.is_file():
            return 'NOT_RECOMPUTED_NO_PREVIOUS_DEEP_CERTIFICATE', None
        _in_tree(cert_path, root / 'runtime' / 'window_attestations' / 'phase364-v1')
        found = _read_json_file(cert_path)
        if found != expected:
            raise ValueError('DEEP_CERTIFICATE_STALE_OR_CONFLICTED')
        return 'PRIOR_DEEP_MATCH_SUPPORTED_BY_LOCAL_CACHE', str(cert_path)
    raise ValueError('UNKNOWN_MODE')


def _heartbeat_paths(root: Path, start_ms: int, end_ms: int) -> list[Path]:
    """Limit daily rotation scan to UTC HOST observation dates only."""
    telemetry = root / 'runtime' / 'telemetry'
    if telemetry.is_symlink():
        raise ValueError('TELEMETRY_DIRECTORY_SYMLINK_FORBIDDEN')
    dates = set()
    day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    last = datetime.fromtimestamp(end_ms / 1000, timezone.utc).date()
    while day <= last:
        dates.add(day.isoformat())
        day += timedelta(days=1)
    files = [telemetry / f'heartbeat-{d}.jsonl' for d in sorted(dates)]
    files.append(telemetry / 'heartbeat.jsonl')
    return [p for p in files if p.is_file() or p.is_symlink()]


def _read_heartbeat_jsonl(path: Path) -> list[dict]:
    if path.is_symlink():
        raise ValueError('TELEMETRY_SYMLINK_FORBIDDEN')
    if path.stat().st_size > MAX_HEARTBEAT_FILE_BYTES:
        raise ValueError('HEARTBEAT_FILE_EXCEEDS_READ_LIMIT')
    for attempt in range(2):
        try:
            data = path.read_bytes()
            if data and not data.endswith(b'\n'):
                if attempt == 0:
                    time.sleep(.05)
                    continue
                raise ValueError('INCOMPLETE_HEARTBEAT_TAIL_RETRY_LATER')
            break
        except PermissionError:
            if attempt:
                raise
            time.sleep(.05)
    rows = []
    for idx, line in enumerate(data.splitlines(), start=1):
        if not line:
            raise ValueError(f'BLANK_HEARTBEAT_LINE: {path.name}:{idx}')
        h = _parse_json_bytes(line)
        if not isinstance(h, dict) or h.get('schema_version') != 'shadow-watchdog-v1':
            raise ValueError(f'INVALID_HEARTBEAT_SCHEMA: {path.name}:{idx}')
        ms, mono = h.get('host_utc_msc'), h.get('monotonic_ns')
        if type(ms) is not int or ms <= 0 or type(mono) is not int or mono <= 0:
            raise ValueError('INVALID_HEARTBEAT_CLOCK_VALUES')
        try:
            stamp = datetime.fromisoformat(h['utc_iso'])
            if stamp.utcoffset() is None:
                raise ValueError('NAIVE_TIMESTAMP')
            stamp_ms = int(stamp.timestamp() * 1000)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ValueError('INVALID_HEARTBEAT_UTC_ISO') from exc
        if abs(stamp_ms - ms) > 2000:
            raise ValueError('HEARTBEAT_TIME_FIELDS_INCONSISTENT')
        if type(h.get('collector_root_count')) is not int or h['collector_root_count'] < 0:
            raise ValueError('INVALID_COLLECTOR_ROOT_COUNT')
        if h.get('collector_alive_bool') not in (None, True, False):
            raise ValueError('INVALID_COLLECTOR_ALIVE_FLAG')
        for key in ('watchdog_interval_overrun_bool', 'wall_clock_shift_suspected_bool'):
            if type(h.get(key)) is not bool:
                raise ValueError(f'INVALID_HEARTBEAT_FLAG_{key}')
        if not isinstance(h.get('alerts'), list) or any(type(x) is not str for x in h['alerts']):
            raise ValueError('INVALID_HEARTBEAT_ALERTS')
        if h.get('timebase_status') != RAW_STATUS or h.get('no_orders') is not True or h.get('original_parquet_unchanged') is not True:
            raise ValueError('HEARTBEAT_SAFETY_FLAGS_INVALID')
        if h.get('mt5_connected_bool') is not None or h.get('mt5_connection_check') != 'NOT_PROBED_NO_MT5_CALLS':
            raise ValueError('UNEXPECTED_MT5_PROBE_CLAIM')
        rows.append(h)
    return rows


def _healthy_pulse(record: dict) -> bool:
    interval_ms = record.get('watchdog_interval_ms')
    # --once writes a null cadence. Two manual pulses must not impersonate an
    # independently running loop. A positive measured interval is mandatory.
    return (type(interval_ms) in (int, float) and 0 < interval_ms <= 25_000
            and record['collector_alive_bool'] is True
            and record['collector_root_count'] == 1
            and record.get('collector_process_scan') == 'COMPLETE'
            and record['watchdog_interval_overrun_bool'] is False
            and record['wall_clock_shift_suspected_bool'] is False
            and record['alerts'] == [])


def correlate_heartbeat(root: Path, host_response_utc_ms: int, batch_id: str,
                        *, before_seconds: int = 30, after_seconds: int = 30,
                        batch_lookup_seconds: int = 180, max_gap_seconds: int = 25) -> dict:
    """Correlate WATCHDOG process visibility around MT5 QUERY RESPONSE end.

    Doesn't prove market sessions, archive commit time, NTP accuracy, MT5
    connectivity, or complete quote capture. Never convert raw broker timestamps.
    """
    earliest = host_response_utc_ms - before_seconds * 1000
    latest = host_response_utc_ms + max(after_seconds, batch_lookup_seconds) * 1000
    selected = _heartbeat_paths(root, earliest, latest)
    records = []
    for path in selected:
        _in_tree(path, root / 'runtime' / 'telemetry')
        for r in _read_heartbeat_jsonl(path):
            if earliest <= r['host_utc_msc'] <= latest:
                records.append(r)
    # watchdog may restart, and multiple files are append-only by day.
    records.sort(key=lambda x: x['host_utc_msc'])
    before = [r for r in records if r['host_utc_msc'] <= host_response_utc_ms]
    after = [r for r in records if host_response_utc_ms <= r['host_utc_msc'] <= host_response_utc_ms + after_seconds * 1000]
    pre = before[-1] if before else None
    post = after[0] if after else None
    batch_seen = [r['host_utc_msc'] for r in records
                  if r['host_utc_msc'] >= host_response_utc_ms and r.get('last_saved_batch_id') == batch_id]
    base = {
        'correlation_target': 'manifest.host_response_end_utc_ms_NOT_PARQUET_COMMIT_TIME',
        'collector_process_only_not_mt5_connectivity': True,
        'utc_dst_certified': False,
        'source_logs': [p.name for p in selected],
        'records_in_scope': len(records),
        'before_utc_ms': pre['host_utc_msc'] if pre else None,
        'after_utc_ms': post['host_utc_msc'] if post else None,
        'batch_id_seen_by_watchdog': bool(batch_seen),
        'first_batch_seen_host_utc_ms': min(batch_seen) if batch_seen else None,
        'max_inter_pulse_seconds': None,
    }
    if pre is None or post is None:
        return dict(base, status='UNVERIFIED_MISSING_BRACKETING_HEARTBEATS')
    in_span = [r for r in records if pre['host_utc_msc'] <= r['host_utc_msc'] <= post['host_utc_msc']]
    if len(in_span) < 2:
        return dict(base, status='UNVERIFIED_INSUFFICIENT_SAMPLES')
    max_gap = max((b['host_utc_msc'] - a['host_utc_msc']) / 1000 for a, b in zip(in_span, in_span[1:]))
    base['max_inter_pulse_seconds'] = round(max_gap, 3)
    if any(b['host_utc_msc'] <= a['host_utc_msc'] or b['monotonic_ns'] <= a['monotonic_ns'] for a,b in zip(in_span,in_span[1:])):
        return dict(base, status='CLOCK_OR_MONOTONIC_SEQUENCE_UNCERTAIN')
    if any(abs((b['host_utc_msc'] - a['host_utc_msc']) -
               (b['monotonic_ns'] - a['monotonic_ns']) / 1_000_000) > 3000
           for a,b in zip(in_span,in_span[1:])):
        return dict(base, status='CLOCK_OR_MONOTONIC_SEQUENCE_UNCERTAIN')
    if any(r['wall_clock_shift_suspected_bool'] for r in in_span):
        return dict(base, status='CLOCK_OR_MONOTONIC_SEQUENCE_UNCERTAIN')
    if max_gap > max_gap_seconds or any(r['watchdog_interval_overrun_bool'] for r in in_span):
        return dict(base, status='WATCHDOG_CADENCE_GAP_DETECTED_CAUSE_UNCLASSIFIED')
    if not all(_healthy_pulse(r) for r in in_span):
        return dict(base, status='COLLECTOR_OR_WATCHDOG_HEALTH_UNVERIFIED_OR_ALERT')
    return dict(base, status='WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE')


def attest_one(root: Path, context: dict, derived: Path, guard, engine, *, mode: str,
               write_deep_certificate: bool = False, correlate: bool = True) -> dict:
    guard.validate_context(context)
    if context['symbol'] not in SYMBOLS or context['permissions']['allow_trade_decisions'] is not False:
        raise ValueError('NO_TRADING_AND_SUPPORTED_SYMBOL_REQUIRED')
    manifest, parquet, manifest_path, provenance = verify_provenance(root, context, engine)
    # Delay the only optional write until after telemetry has parsed cleanly.
    metric_status, cert_path = verify_metric_evidence(root, context, derived, provenance,
                                                      manifest, parquet, engine, mode, False)
    heartbeat = (correlate_heartbeat(root, manifest['host_response_end_utc_ms'], manifest['capture_batch_id'])
                 if correlate else {'status': 'NOT_CHECKED_TEST_ONLY'})
    if write_deep_certificate:
        if mode != 'deep':
            raise ValueError('FAST_MODE_CANNOT_CREATE_DEEP_CERTIFICATE')
        certificate = _expected_certificate(root, context, derived, provenance)
        cert = _certificate_path(root, context)
        _save_immutable_certificate(cert, certificate)
        cert_path = str(cert)
    return {
        'symbol': context['symbol'], 'window_id': context['window_id'],
        'provenance': provenance,
        'metrics_verification': metric_status,
        'certificate_path': cert_path,
        'heartbeat': heartbeat,
        'source_context_quality_unchanged': context['quality']['flag'],
        'source_context_heartbeat_unchanged': context['quality']['heartbeat_status'],
        'timebase_status': context['timebase_status'],
        'research_only': True, 'no_orders': True,
        'ready_for_model_publication': False,
        'evidence_complete_for_this_phase': (
            metric_status in ('RECOMPUTED_EXACT_MATCH', 'PRIOR_DEEP_MATCH_SUPPORTED_BY_LOCAL_CACHE')
            and heartbeat['status'] == 'WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE'),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Phase 3.6.4 offline provenance + heartbeat, no trading')
    p.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--symbols', nargs='+', choices=SYMBOLS, default=list(SYMBOLS))
    p.add_argument('--recent', type=int, default=1, help='Most recent N saved derived windows per symbol (1..30)')
    p.add_argument('--mode', choices=('deep','fast'), default='deep',
                   help='deep: recompute via Polars; fast: check only prior deep certificate and SHA')
    p.add_argument('--write-deep-certificate', action='store_true',
                   help='Create immutable local evidence cache; never alters Shadow or derived JSON')
    args = p.parse_args(argv)
    if not 1 <= args.recent <= MAX_WINDOWS_PER_SYMBOL:
        p.error('--recent must be 1..30')
    if args.write_deep_certificate and args.mode != 'deep':
        p.error('--write-deep-certificate requires --mode deep')
    try:
        root = args.project_root.resolve()
        guard = _module(root / 'phase363_contract_v2' / 'strict_guard.py', 'phase364_strict_guard')
        engine = _module(root / 'scripts' / 'phase363_polars_engine.py', 'phase364_polars_engine')
        results = []
        for symbol in dict.fromkeys(args.symbols):
            for context, derived in contexts_to_check(root, symbol, args.recent, guard):
                results.append(attest_one(root, context, derived, guard, engine, mode=args.mode,
                                          write_deep_certificate=args.write_deep_certificate))
        complete = bool(results) and all(x['evidence_complete_for_this_phase'] for x in results)
        out = {
            'status': 'RESEARCH_ATTESTATION_EVIDENCE_COMPLETE' if complete else 'RESEARCH_ATTESTATION_PARTIAL',
            'schema_version': 'phase364-research-attestation-v1',
            'mode': args.mode,
            'not_real_qwen_output': True,
            'timebase_certified': False,
            'market_sessions_certified': False,
            'mt5_connection_probed': False,
            'no_discord_messages': True,
            'no_orders': True,
            'raw_and_derived_sources_unchanged': True,
            'only_new_files_possible': ('DEEP_CERTIFICATE_ONLY' if args.write_deep_certificate else 'NONE'),
            'results': results,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if complete else 3
    except (ValueError, RuntimeError, ImportError, OSError, KeyError, TypeError,
            AssertionError, json.JSONDecodeError) as exc:
        print('PHASE364_ATTESTATION_FAIL_CLOSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
