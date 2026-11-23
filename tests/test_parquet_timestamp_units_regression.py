"""Regression for timestamp[ms, UTC] read back through Pandas/PyArrow.

This is a REAL Parquet test: it deliberately fails if pyarrow is missing,
ensuring that no one certifies collection based only on mock tests.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from mt5_agents.parquet_integrity import canonical_frame, write_verified_parquet, verify_parquet


def sample_ticks():
    ms = int(datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    return pd.DataFrame({
        "time_msc": np.array([ms, ms, ms + 1, ms + 17], dtype="int64"),
        "bid": np.array([4100.0, 4100.01, 4100.02, 4100.03], dtype="float64"),
        "ask": np.array([4100.2, 4100.21, 4100.22, 4100.23], dtype="float64"),
        "flags": np.array([2, 6, 2, 6], dtype="int32"),
        "spread_points": np.array([20.] * 4, dtype="float64"),
    })


def test_real_parquet_utc_milliseconds_survive_roundtrip(tmp_path):
    output = tmp_path / "ticks.parquet"
    source = sample_ticks()
    write_verified_parquet(source, output)  # includes production verifier
    report = verify_parquet(output, expected=canonical_frame(source))
    assert report["rows"] == 4 and report["timezone"] == "UTC"
    table = pq.ParquetFile(output).read()
    assert table.schema.field("timestamp_utc").type == pa.timestamp("ms", tz="UTC")
    native_timestamp_ms = table.column("timestamp_utc").cast(pa.int64()).to_pylist()
    assert native_timestamp_ms == source["time_msc"].tolist()
    assert native_timestamp_ms[0] == native_timestamp_ms[1]
    assert native_timestamp_ms[2] == native_timestamp_ms[0] + 1


def test_same_schema_but_mismatched_timestamp_is_rejected(tmp_path):
    output = tmp_path / "ticks.parquet"
    write_verified_parquet(sample_ticks(), output)
    table = pq.ParquetFile(output).read()
    pos = table.schema.get_field_index("timestamp_utc")
    wrong_ms = table.column("time_msc").to_numpy(zero_copy_only=False).copy()
    wrong_ms[2] += 1  # still a valid timestamp[ms, UTC], but no longer the real tick time
    tampered_ts = pa.array(wrong_ms, type=pa.int64()).cast(pa.timestamp("ms", tz="UTC"))
    tampered = table.set_column(pos, table.schema.field(pos), tampered_ts)
    pq.write_table(tampered, output, compression="zstd", version="2.6")
    with pytest.raises(RuntimeError, match="timestamp does not match"):
        verify_parquet(output)
