from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from hashlib import sha256
import json
import os

import pytest

from mt5_agents.phase36_session_coverage import (
    audit, _session_labels, _session_local_date, _rollover_candidate,
)


def utc_ms(s):
    return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()*1000)

Z = {k: ZoneInfo(k) for k in ('Asia/Tokyo', 'Europe/London', 'America/New_York')}


def test_overlap_winter_and_summer_dst():
    for date in ('2026-11-23T13:15:00Z', '2026-07-01T12:15:00Z'):
        labels = _session_labels(utc_ms(date), Z)
        assert 'LONDON' in labels and 'NEW_YORK' in labels
        assert 'LONDON_NY_OVERLAP' in labels


def test_session_days_use_local_calendar_not_raw_broker_date():
    point = utc_ms('2026-11-22T23:45:00Z')
    assert _session_local_date(point, 'ASIA_TOKYO', Z) == '2026-11-23'
    assert _session_local_date(point, 'NEW_YORK', Z) == '2026-11-22'


def test_rollover_is_a_candidate_only():
    assert _rollover_candidate(utc_ms('2026-11-23T22:00:00Z'), Z)
    assert not _rollover_candidate(utc_ms('2026-11-23T21:00:00Z'), Z)


def _mk(root, start, symbol='XAUUSD', host_gap=0, offset=7200, count=10):
    end = start+60_000
    d = root/'parquet'/'identity=test20' / f'symbol={symbol}' / 'raw_date=2026-11-23'
    d.mkdir(parents=True, exist_ok=True)
    parquet = d/f'raw_{start}_{end}.parquet'
    parquet.write_bytes(f'sample {start}'.encode())
    host = end-offset*1000+12000+host_gap
    payload = {
        'schema_version':'shadow-v1', 'timebase_status':'UNVERIFIED_PENDING_DST',
        'utc_time_certified':False, 'dst_certified':False,
        'symbol':symbol, 'identity_tag':'test20', 'broker_offset_candidate_sec':offset,
        'window_start_raw_ms':start, 'window_end_raw_ms_exclusive':end,
        'host_request_start_utc_ms':host-400, 'host_response_end_utc_ms':host,
        'count':count, 'sha256':sha256(parquet.read_bytes()).hexdigest(),
    }
    parquet.with_suffix('.manifest.json').write_text(json.dumps(payload))
    return parquet


def test_no_archive_and_no_certification(tmp_path):
    r=audit(tmp_path)
    assert r['assessment']=='NO_DATA' and not r['five_liquid_sessions_certified']
    assert not r['utc_dst_certified']


def test_integrity_and_session_report_is_read_only(tmp_path):
    t = utc_ms('2026-11-23T13:00:00Z')+7200*1000
    p=_mk(tmp_path,t)
    before=p.read_bytes()
    out=audit(tmp_path)
    assert out['assessment']=='PROVISIONAL_COVERAGE_ONLY'
    assert out['manifest_count']==1 and out['validated_window_count']==1
    assert out['total_recorded_quote_ticks']==10
    assert out['groups'][0]['candidate_session_minutes_not_certified']['LONDON_NY_OVERLAP']==1
    assert not out['five_liquid_sessions_certified']
    assert p.read_bytes()==before


def test_windows_gap_not_claimed_sleep(tmp_path):
    start=utc_ms('2026-11-23T13:00:00Z')+7200*1000
    _mk(tmp_path,start)
    _mk(tmp_path,start+3*60_000,host_gap=600_000)
    g=audit(tmp_path)['groups'][0]
    assert g['missing_window_minutes_unclassified']==2
    assert g['host_observation_gaps_over_130s_count']==1
    assert 'NOT_PROVEN_SLEEP' in g['host_observation_gaps_first_25'][0]['state']


def test_tampering_is_detected(tmp_path):
    p=_mk(tmp_path,utc_ms('2026-11-23T13:00:00Z')+7200*1000)
    p.write_bytes(b'tampered')
    result=audit(tmp_path)
    assert result['assessment']=='AUDIT_FAILED'
    assert result['validated_window_count']==0
    assert result['errors'][0]['code']=='INVALID_MANIFEST_OR_PARQUET'


def test_recent_orphan_is_not_deemed_permanent_corruption(tmp_path):
    path=tmp_path/'parquet'/'inflight.parquet'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'not finished')
    result=audit(tmp_path)
    assert result['assessment']=='NO_DATA'
    assert result['warnings'][0]['code']=='INFLIGHT_RECENT_ORPHAN'


def test_parquet_footer_verification_optional(tmp_path):
    pq=pytest.importorskip('pyarrow.parquet')
    pa=pytest.importorskip('pyarrow')
    start=utc_ms('2026-11-23T13:00:00Z')+7200*1000
    path=_mk(tmp_path,start,count=3)
    # This is deliberately NOT a valid Parquet file -- deep read must report error.
    result=audit(tmp_path,deep=True)
    assert result['assessment']=='AUDIT_FAILED'
