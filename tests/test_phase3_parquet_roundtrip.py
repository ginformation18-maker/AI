"""MANDATORY REAL Parquet integration gate. Intentionally NEVER skip when pyarrow absent."""
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import pytest

from mt5_agents.parquet_integrity import canonical_frame, write_verified_parquet, verify_parquet
from mt5_agents.tick_collector import TickArchive
from test_phase3_tick_collector import Broker, BASE


def _frame():
    ts = int(datetime(2026, 11, 20, 9, 10, 0, tzinfo=timezone.utc).timestamp()*1000)
    return pd.DataFrame({
        "time_msc":np.array([ts,ts,ts+1,ts+123],dtype="int64"),
        "bid":np.array([4000,4000.01,4000.02,4000.03],dtype="float64"),
        "ask":np.array([4000.1,4000.11,4000.12,4000.13],dtype="float64"),
        "flags":np.array([6,6,6,6],dtype="int32"),
        "spread_points":np.array([10,10,10,10],dtype="float64"),
    })


def test_real_compressed_parquet_roundtrip_schema_utc_milliseconds_and_duplicates(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    assert pa is not None and pq is not None
    path=tmp_path/'ticks.parquet'
    original=_frame()
    write_verified_parquet(original, path)
    result=verify_parquet(path, expected=canonical_frame(original))
    assert result['rows']==4
    assert result['timestamp_precision']=='milliseconds'
    raw=pq.ParquetFile(path).read()
    assert raw.schema.field('timestamp_utc').type==pa.timestamp('ms',tz='UTC')
    assert raw.schema.field('time_msc').type==pa.int64()
    assert raw.schema.field('flags').type==pa.int32()
    assert raw.schema.field('bid').type==pa.float64()
    assert raw.column('time_msc').to_pylist()[0]==raw.column('time_msc').to_pylist()[1]


def test_bad_timestamp_float_rejected_before_write(tmp_path):
    bad=_frame()
    bad['time_msc']=bad['time_msc'].astype('float64')
    with pytest.raises(ValueError,match='lossless integer'):
        write_verified_parquet(bad,tmp_path/'bad.parquet')
    assert not (tmp_path/'bad.parquet').exists()


def test_corrupted_real_parquet_fails_integrity_check(tmp_path):
    path=tmp_path/'ticks.parquet'
    write_verified_parquet(_frame(),path)
    path.write_bytes(path.read_bytes()[:80])
    with pytest.raises(Exception):
        verify_parquet(path)


def test_collector_writes_and_reads_real_parquet_with_manifest(tmp_path):
    a=TickArchive(tmp_path,Broker(),123,'DemoServer')
    result=a.collect('XAUUSD',BASE,BASE+timedelta(seconds=60))
    assert result['count']==2
    meta=verify_parquet(result['path'])
    assert meta['rows']==2
    assert meta['first_ms']==int(BASE.timestamp()*1000)
    assert a.collect('XAUUSD',BASE,BASE+timedelta(seconds=60))['already_indexed']
