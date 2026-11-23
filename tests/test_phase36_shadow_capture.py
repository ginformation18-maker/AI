from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import numpy as np
import pytest

from mt5_agents.phase36_shadow_capture import (
    STATUS, ShadowCapture, capture_status, checked_offset, exclusive_shadow_process,
    filter_and_validate_ticks, last_closed_raw_window, safe_symbol,
)

RAW_START = 1795431840000  # nominal raw broker time; absolute UTC meaning not asserted
RAW_END = RAW_START + 60_000
HOST_MS = RAW_END + 15_000 - 7_200_000


def rows(n=3):
    x = np.zeros(n, dtype=[('time', '<i8'), ('bid','<f8'), ('ask','<f8'),
                           ('last','<f8'), ('volume','<u8'), ('time_msc','<i8'),
                           ('flags','<u4'), ('volume_real','<f8')])
    x['time_msc'] = np.array([RAW_START+1_000, RAW_START+2_000, RAW_START+50_000][:n], dtype='int64')
    x['time'] = x['time_msc']//1000
    x['bid'] = 3600 + np.arange(n) * 0.01
    x['ask'] = x['bid'] + 0.15
    x['flags'] = 1158
    return x


class Broker:
    ACCOUNT_TRADE_MODE_DEMO = 0
    COPY_TICKS_ALL = 0

    def __init__(self, ticks=None, newest=None):
        self.ticks = ticks if ticks is not None else rows()
        self.newest = newest if newest is not None else RAW_END + 14_000
        self.account = SimpleNamespace(login=123456, server='MetaQuotes-Demo', trade_mode=0)

    def account_info(self):
        return self.account

    def symbol_select(self, symbol, yes):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(point=0.01)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time_msc=self.newest, time=self.newest//1000,
                               bid=3600, ask=3600.15)

    def copy_ticks_range(self, symbol, date_from, date_to, flags):
        assert date_from.tzinfo == timezone.utc
        assert date_to.tzinfo == timezone.utc
        assert int(date_from.timestamp() * 1000) == RAW_START
        assert int(date_to.timestamp() * 1000) == RAW_END
        return self.ticks


def new_capture(tmp_path, fake=None):
    return ShadowCapture(root=tmp_path, mt5=fake if fake is not None else Broker(),
                         server='MetaQuotes-Demo', login=123456, offset_sec=7200)


def test_offset_candidate_is_not_certified():
    assert checked_offset(7_200_000 + 999, 1000, 7200) == 0.001
    with pytest.raises(RuntimeError, match='STALE'):
        checked_offset(7_200_000 - 30_000, 0, 7200)
    with pytest.raises(RuntimeError, match='STALE'):
        checked_offset(7_200_000 + 3_000, 0, 7200)


def test_window_is_closed_with_finality():
    start, end = last_closed_raw_window(HOST_MS, 7200)
    assert (start, end) == (RAW_START, RAW_END)
    assert last_closed_raw_window(HOST_MS - 10_000, 7200) == (RAW_START-60_000, RAW_START)


def test_filters_inclusive_upper_bound_no_time_change():
    arr = np.concatenate([rows(), rows(1)])
    arr[-1]['time_msc'] = RAW_END
    assert len(filter_and_validate_ticks(arr, RAW_START, RAW_END)) == 3


def test_no_reordering_of_broker_rows():
    arr = rows()[::-1].copy()
    with pytest.raises(RuntimeError, match='Out-of-order'):
        filter_and_validate_ticks(arr, RAW_START, RAW_END)


def test_same_millisecond_duplicate_kept():
    arr = rows()
    arr['time_msc'][1] = arr['time_msc'][0]
    arr['time'][1] = arr['time'][0]
    assert len(filter_and_validate_ticks(arr, RAW_START, RAW_END)) == 3


def test_reject_invalid_quotes_and_inconsistent_fields():
    arr = rows()
    arr['ask'][0] = arr['bid'][0] - 1
    with pytest.raises(RuntimeError, match='Invalid'):
        filter_and_validate_ticks(arr, RAW_START, RAW_END)
    arr = rows()
    arr['time'][0] -= 20
    with pytest.raises(RuntimeError, match='Inconsistent'):
        filter_and_validate_ticks(arr, RAW_START, RAW_END)


def test_no_ticks_is_not_valid_coverage():
    assert len(filter_and_validate_ticks(rows()[:0], RAW_START, RAW_END)) == 0


def test_symbol_traversal_rejected():
    with pytest.raises(ValueError): safe_symbol('../file')
    assert safe_symbol('XAUUSD.a') == 'XAUUSD.a'


def test_account_switch_rejected(tmp_path):
    fake = Broker()
    fake.account.server = 'Other-Server'
    with pytest.raises(RuntimeError, match='account'):
        new_capture(tmp_path, fake).capture_once('XAUUSD', host_ms=HOST_MS)


def test_stale_feed_does_not_write(tmp_path):
    fake = Broker(newest=RAW_END-200_000)
    result = new_capture(tmp_path, fake).capture_once('XAUUSD', host_ms=HOST_MS)
    assert result['state'] == 'STALE_OR_TIMEBASE_MISMATCH'
    assert not list(tmp_path.rglob('*.parquet'))


def test_lock_rejects_second_instance(tmp_path):
    with exclusive_shadow_process(tmp_path):
        with pytest.raises(RuntimeError, match='lock exists'):
            with exclusive_shadow_process(tmp_path): pass
    assert not (tmp_path/'.shadow_capture.lock').exists()


def test_status_empty(tmp_path):
    x = capture_status(tmp_path)
    assert x['archived_raw_windows'] == 0
    assert x['five_sessions_certified'] is False


def test_write_and_idempotent(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    monkeypatch.setattr('mt5_agents.phase36_shadow_capture.time.time_ns', lambda: HOST_MS * 1_000_000)
    fake = Broker()
    cap = new_capture(tmp_path, fake)
    r = cap.capture_once('XAUUSD', host_ms=HOST_MS)
    assert r['state'] == 'SHADOW_RAW_CAPTURED_UNVERIFIED'
    assert r['count'] == 3
    q = cap.capture_once('XAUUSD', host_ms=HOST_MS)
    assert q['state'] == 'ALREADY_VERIFIED'
    paths = list(tmp_path.rglob('*.parquet'))
    assert len(paths) == 1
    import pyarrow.parquet as pq
    table = pq.ParquetFile(paths[0]).read()
    assert table['raw_time_msc'].to_pylist() == list(map(int, rows()['time_msc']))
    assert 'timestamp_utc' not in table.column_names
    assert table['timebase_status'].to_pylist() == [STATUS] * 3
    assert table['broker_offset_candidate_sec'].to_pylist() == [7200] * 3
    assert len(set(table['capture_batch_id'].to_pylist())) == 1
    assert capture_status(tmp_path)['rows'] == 3
    assert capture_status(tmp_path)['five_sessions_certified'] is False


def test_detects_file_corruption(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    monkeypatch.setattr('mt5_agents.phase36_shadow_capture.time.time_ns', lambda: HOST_MS * 1_000_000)
    cap = new_capture(tmp_path)
    cap.capture_once('XAUUSD', host_ms=HOST_MS)
    path = next(tmp_path.rglob('*.parquet'))
    with path.open('ab') as stream: stream.write(b'bad')
    with pytest.raises(RuntimeError, match='checksum'):
        cap.capture_once('XAUUSD', host_ms=HOST_MS)


def test_missing_manifest_refuses_overwrite(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    monkeypatch.setattr('mt5_agents.phase36_shadow_capture.time.time_ns', lambda: HOST_MS * 1_000_000)
    cap = new_capture(tmp_path)
    cap.capture_once('XAUUSD', host_ms=HOST_MS)
    next(tmp_path.rglob('*.manifest.json')).unlink()
    with pytest.raises(RuntimeError, match='Orphan'):
        cap.capture_once('XAUUSD', host_ms=HOST_MS)


def test_empty_rows_are_recorded_as_unverified_not_filled(tmp_path, monkeypatch):
    monkeypatch.setattr('mt5_agents.phase36_shadow_capture.time.time_ns', lambda: HOST_MS * 1_000_000)
    fake = Broker(ticks=rows()[:0])
    result = new_capture(tmp_path, fake).capture_once('XAUUSD', host_ms=HOST_MS)
    assert result['state'] == 'NO_TICKS_UNVERIFIED'
    assert not list(tmp_path.rglob('*.parquet'))
    assert len(list(tmp_path.rglob('*.no_ticks_unverified.json'))) == 1


def test_windows_time_check_rejects_unsynced(monkeypatch):
    import mt5_agents.phase36_shadow_capture as mod
    monkeypatch.setattr(mod.os, 'name', 'nt')
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0,
        stdout='Leap Indicator: 3(not synchronized)\nSource: Local CMOS Clock'))
    with pytest.raises(RuntimeError, match='not confirmed synchronized'):
        mod.require_windows_ntp_sync()


def test_windows_time_check_accepts_synchronized(monkeypatch):
    import mt5_agents.phase36_shadow_capture as mod
    monkeypatch.setattr(mod.os, 'name', 'nt')
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0,
        stdout='Leap Indicator: 0(no warning)\nSource: time.windows.com,0x9'))
    mod.require_windows_ntp_sync()


def test_status_blocks_orphan_parquet(tmp_path):
    path = tmp_path/'parquet'/'raw_unverified.parquet'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'arbitrary')
    with pytest.raises(RuntimeError, match='orphan'):
        capture_status(tmp_path)


def test_status_blocks_partial_files(tmp_path):
    path = tmp_path/'parquet'/'unfinished.partial'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'arbitrary')
    with pytest.raises(RuntimeError, match='partial'):
        capture_status(tmp_path)
