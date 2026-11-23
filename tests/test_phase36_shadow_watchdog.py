"""Standalone watchdog safety tests; no MetaTrader5 dependency or broker calls."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import importlib.util
import json

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'phase36_shadow_watchdog.py'
spec = importlib.util.spec_from_file_location('standalone_phase36_watchdog', SOURCE)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def test_loop_command_matching_not_once():
    assert w._collector_command(['python.exe', 'scripts\\phase36_shadow_capture.py', '--loop', '--symbols', 'XAUUSD'])
    assert not w._collector_command(['python.exe', 'scripts\\phase36_shadow_capture.py', '--once'])
    assert not w._collector_command(['python.exe', 'scripts\\phase36_shadow_watchdog.py', '--loop'])


def test_root_restricts_other_projects(tmp_path):
    root = tmp_path / 'one'
    root.mkdir()
    rec = {'cmdline': ['python.exe','scripts/phase36_shadow_capture.py','--loop'], 'cwd': str(root)}
    assert w.process_matches_root(rec, root)
    assert not w.process_matches_root(rec, tmp_path / 'other')


def test_parent_child_not_double_counted():
    entries = [{'pid': 32028, 'ppid': 29436}, {'pid': 2408, 'ppid': 32028}]
    pids = {e['pid'] for e in entries}
    roots = [e for e in entries if e['ppid'] not in pids]
    assert len(roots) == 1


def test_monotonic_interval_and_clock_step():
    got = w.classify_interval(1_000_000_000, 17_000_000_000, 10,
                              1_000_000_000, 19_500_000_000)
    assert got['watchdog_interval_overrun_bool']
    assert got['wall_clock_shift_suspected_bool'] is False
    assert got['watchdog_interval_ms'] == 16000.0
    got = w.classify_interval(0, 10_000_000_000, 10, 0, 18_000_000_000)
    assert got['wall_clock_shift_suspected_bool']


def test_build_heartbeat_reports_unknown_mt5():
    now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    interval = w.classify_interval(None, 1, 10)
    proc = {'collector_alive_bool': True, 'collector_pids': [1, 2],
            'collector_root_count': 1, 'collector_process_scan': 'COMPLETE'}
    batch = {'last_saved_batch_id':'batch1', 'last_saved_utc_ms': int(now.timestamp()*1000)-1000,
             'last_manifest_scan':'OK'}
    result = w.build_heartbeat(now, 1, 1, proc, batch, interval)
    assert result['collector_alive_bool'] is True
    assert result['mt5_connected_bool'] is None
    assert result['lag_ms'] == 1000
    assert result['no_orders'] and result['timebase_status'] == 'UNVERIFIED_PENDING_DST'


def test_no_recent_batch_not_called_sleep():
    now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    interval = w.classify_interval(None, 1, 10)
    batch = {'last_saved_batch_id':'b', 'last_saved_utc_ms':int(now.timestamp()*1000)-181000,
             'last_manifest_scan':'OK'}
    proc = {'collector_alive_bool':True, 'collector_pids':[1], 'collector_root_count':1, 'collector_process_scan':'COMPLETE'}
    record = w.build_heartbeat(now, 1, 1, proc, batch, interval)
    assert 'NO_RECENT_SAVED_BATCH_MARKET_OR_CAPTURE_UNCLASSIFIED' in record['alerts']
    assert not any('SLEEP' in a for a in record['alerts'])


def test_manifest_scan_reads_only_sidecar(tmp_path):
    p = tmp_path / 'parquet' / 'symbol=XAUUSD'
    p.mkdir(parents=True)
    manifest = p/'raw_1_2.manifest.json'
    manifest.write_text(json.dumps({'host_response_end_utc_ms': 1500,'capture_batch_id':'batch-sample'}))
    result = w.latest_saved_batch(tmp_path)
    assert result['last_saved_batch_id'] == 'batch-sample'
    assert result['last_saved_utc_ms'] == 1500


def test_log_append_and_status(tmp_path):
    path = tmp_path/'heartbeat.jsonl'
    lock = tmp_path/'.lock'
    rec = {'utc_iso':'2026-11-23T09:00:00.000+00:00', 'a':1}
    w.append_heartbeat(rec, path)
    w.append_heartbeat(rec, path)
    assert len(path.read_text().splitlines()) == 2
    assert w._last_line(path)['a'] == 1
    assert w.report_status(path, lock)['watchdog_lock_exists'] is False


def test_log_daily_rotation_never_overwrite(tmp_path):
    path = tmp_path/'heartbeat.jsonl'
    w.append_heartbeat({'utc_iso':'2026-11-23T23:59:59.000+00:00'}, path)
    w.append_heartbeat({'utc_iso':'2026-11-24T00:00:00.000+00:00'}, path)
    assert (tmp_path/'heartbeat-2026-11-23.jsonl').exists()
    assert len(path.read_text().splitlines()) == 1
    assert '2026-11-24' in path.read_text()


def test_double_lock_blocked(tmp_path):
    lock = tmp_path/'.lock'
    with w.single_watchdog(lock):
        with pytest.raises(RuntimeError, match='Watchdog lock exists'):
            with w.single_watchdog(lock):
                pass
    assert not lock.exists()
