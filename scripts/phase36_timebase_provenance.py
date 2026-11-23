"""Phase 3.6 broker-timebase provenance audit; no broker order or archive writes.

Requirements: the existing .venv, .env, PyArrow, and MetaTrader5.
This tool intentionally NEVER certifies historical UTC from a current-day offset.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def iso_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(int(epoch_ms) / 1000, timezone.utc).isoformat(timespec='milliseconds')


def candidate_offset(deltas: list[float], bucket=900, tolerance=20) -> dict:
    if len(deltas) < 2:
        return {'status': 'INSUFFICIENT_SAMPLES', 'candidate_offset_seconds': None}
    nearest = int(round(statistics.median(deltas) / bucket) * bucket)
    plausible = 900 <= abs(nearest) <= 14 * 3600
    consistent = plausible and all(abs(d - nearest) <= tolerance for d in deltas)
    return {
        'status': 'CURRENT_SESSION_CANDIDATE_ONLY' if consistent else 'UNKNOWN_OR_INCONSISTENT',
        'candidate_offset_seconds': nearest if consistent else None,
        'minimum_delta_seconds': round(min(deltas), 3),
        'maximum_delta_seconds': round(max(deltas), 3),
        'sample_count': len(deltas),
        'historical_offset_certified': False,
    }


def archive_integrity(ms, utc_ms, bid, ask) -> dict:
    if not (len(ms) == len(utc_ms) == len(bid) == len(ask)):
        return {'valid': False, 'reason': 'MISMATCHED_COLUMN_LENGTHS'}
    if not ms:
        return {'valid': False, 'reason': 'EMPTY_ARCHIVE'}
    return {
        'valid': (all(int(m) == int(u) for m, u in zip(ms, utc_ms))
                  and all(b >= a for a, b in zip(ms, ms[1:]))
                  and all(b != a for a, b in zip(ms, ms[1:]))
                  and all(b > 0 and a >= b for b, a in zip(bid, ask))),
        'rows': len(ms),
        'timestamp_columns_equal': all(int(m) == int(u) for m, u in zip(ms, utc_ms)),
        'out_of_order_count': sum(int(b < a) for a, b in zip(ms, ms[1:])),
        'duplicate_adjacent_milliseconds': sum(int(b == a) for a, b in zip(ms, ms[1:])),
        'invalid_bid_ask_count': sum(int(b <= 0 or a <= 0 or a < b) for b, a in zip(bid, ask)),
        'first_raw_label_not_certified_utc': iso_ms(ms[0]),
        'last_raw_label_not_certified_utc': iso_ms(ms[-1]),
    }


def live_probe(mt5, symbols: tuple[str, ...]) -> dict:
    samples = []
    deltas = []
    advances = {}
    for attempt in range(3):
        for symbol in symbols:
            wall = time.time()
            tick = mt5.symbol_info_tick(symbol)
            if tick is None or not getattr(tick, 'time_msc', None) or tick.bid <= 0 or tick.ask < tick.bid:
                samples.append({'symbol': symbol, 'status': 'INVALID_OR_MISSING'})
                continue
            raw = int(tick.time_msc)
            delta = raw / 1000 - wall
            deltas.append(delta)
            samples.append({'symbol': symbol, 'status': 'OBSERVED', 'raw_time_msc': raw,
                            'raw_label_not_certified_utc': iso_ms(raw),
                            'delta_vs_pc_utc_seconds': round(delta, 3)})
        if attempt < 2:
            time.sleep(1.0)
    for symbol in symbols:
        advances[symbol] = len({x['raw_time_msc'] for x in samples
                                if x['symbol'] == symbol and 'raw_time_msc' in x}) > 1
    analysis = candidate_offset(deltas)
    analysis['ticks_advanced_during_test'] = advances
    if not all(advances.values()):
        analysis['status'] = 'MARKET_IDLE_OR_MISSING'
    return {'clock_analysis': analysis, 'samples': samples}


def inspect_archive(path, pq, pa, broker, account_login: int, server_tag: str,
                    offset: int | None, max_history_ticks: int = 12000) -> dict:
    # This performs reads only. It does not change any stored timestamp or Parquet bytes.
    partition = {p for p in path.parts if p.startswith(('login=', 'server='))}
    expected = {f'login={account_login}', f'server={server_tag}'}
    identity_match = expected.issubset(partition)
    result = {
        'file': path.name,
        'symbol_partition': next((part[7:] for part in path.parts if part.startswith('symbol=')), None),
        'date_partition': next((part[5:] for part in path.parts if part.startswith('date=')), None),
        'same_connected_account_and_server': identity_match,
        'size_bytes': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'historical_timebase_status': 'UNVERIFIED',
    }
    table = pq.read_table(path, columns=['time_msc', 'timestamp_utc', 'bid', 'ask'])
    ms = table['time_msc'].to_pylist()
    utc_ms = table['timestamp_utc'].cast(pa.timestamp('ms', tz='UTC')).cast(pa.int64()).to_pylist()
    bid = table['bid'].to_pylist()
    ask = table['ask'].to_pylist()
    result['integrity'] = archive_integrity(ms, utc_ms, bid, ask)
    if offset is not None and ms:
        result['candidate_utc_first_if_same_historical_offset'] = iso_ms(min(ms) - offset * 1000)
        result['candidate_utc_last_if_same_historical_offset'] = iso_ms(max(ms) - offset * 1000)
        result['candidate_is_not_historical_proof'] = True
    symbol = result['symbol_partition']
    if not identity_match or not symbol or not ms or len(ms) > max_history_ticks:
        result['broker_history_match'] = {'status': 'SKIPPED_IDENTITY_EMPTY_OR_SIZE_LIMIT'}
        return result
    start = datetime.fromtimestamp((min(ms)-1000) / 1000, timezone.utc)
    end = datetime.fromtimestamp((max(ms)+1000) / 1000, timezone.utc)
    history = broker.copy_ticks_range(symbol, start, end, broker.COPY_TICKS_ALL)
    if history is None:
        result['broker_history_match'] = {'status': 'QUERY_ERROR', 'error_code': broker.last_error()[0]}
        return result
    expected_ticks = {(int(t), round(float(b), 8), round(float(a), 8))
                      for t, b, a in zip(ms, bid, ask)}
    returned_ticks = {(int(t['time_msc']), round(float(t['bid']), 8), round(float(t['ask']), 8))
                      for t in history}
    result['broker_history_match'] = {
        'status': 'MATCHED_RAW_RECORDS_NOT_UTC_CERTIFICATION',
        'archived_unique_tuples': len(expected_ticks),
        'matching_unique_tuples': len(expected_ticks & returned_ticks),
        'missing_unique_tuples': len(expected_ticks - returned_ticks),
        'queried_broker_rows': len(history),
    }
    return result


def main() -> int:
    result = {
        'tool': 'Phase 3.6 Read-Only Timebase Provenance Audit',
        'read_only': True,
        'modifies_archive': False,
        'sends_orders': False,
        'historical_offset_certified': False,
        'utc_clock_at_start': datetime.now(timezone.utc).isoformat(),
    }
    try:
        from mt5_agents.settings import Settings
        from mt5_agents.market import connect_mt5
        from mt5_agents.observation_timebase import ObservationTimebase
        import MetaTrader5 as mt5
        import pyarrow as pa
        import pyarrow.parquet as pq

        cfg = Settings.load()
        if cfg.mode != 'DRY_RUN':
            raise RuntimeError('MODE_MUST_BE_DRY_RUN')
        timebase = ObservationTimebase.from_env(cfg)
        if not timebase.provisional:
            raise RuntimeError('PROVISIONAL_OBSERVATION_REQUIRED')
        result['configured_provisional_offset_seconds'] = timebase.offset_seconds
        account = connect_mt5(cfg)
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO or account.server != cfg.server:
            raise RuntimeError('DEMO_ACCOUNT_SERVER_IDENTITY_FAILED')
        result['broker_server'] = cfg.server
        result['terminal_version'] = list(mt5.version())
        report_live = live_probe(mt5, cfg.symbols)
        result['current_session'] = report_live
        measured = report_live['clock_analysis']['candidate_offset_seconds']
        result['offset_matches_provisional_config'] = (
            measured is not None and measured == timebase.offset_seconds
            and report_live['clock_analysis']['status'] == 'CURRENT_SESSION_CANDIDATE_ONLY'
        )
        root = ROOT / 'runtime' / 'tick_archive' / 'parquet'
        paths = sorted(root.rglob('*.parquet')) if root.is_dir() else []
        result['archive_files_found'] = len(paths)
        tag = hashlib.sha256(cfg.server.encode('utf-8')).hexdigest()[:12]
        result['archive_files_audited'] = [inspect_archive(p, pq, pa, mt5, account.login, tag,
                                                           timebase.offset_seconds) for p in paths[:10]]
        if len(paths) > 10:
            result['archives_not_audited_due_to_limit'] = len(paths)-10
        result['status'] = 'OBSERVATION_AUDIT_ONLY_NOT_APPROVED_FOR_ARCHIVE_OR_TRADING'
        result['next_gate'] = ('Independent historic offset/DST evidence and schema/version design '
                               'are still required before any corrected UTC archival writes.')
        exit_code = 0 if result['offset_matches_provisional_config'] else 2
    except Exception as exc:
        result['status'] = 'FAIL_CLOSED'
        result['error_type'] = type(exc).__name__
        # Avoid accidentally printing login, password, or environment values in exceptions.
        result['operator_message'] = 'Check demo account, .env provisional timebase settings, and MT5 connectivity.'
        exit_code = 2
    finally:
        try:
            import MetaTrader5 as mt5
            mt5.shutdown()
        except Exception:
            pass
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
