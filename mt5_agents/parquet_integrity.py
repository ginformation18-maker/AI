"""Strict, end-to-end tick Parquet contract. No silently skipped integration gate.

The original MT5 millisecond timestamp is the canonical int64 time_msc. The
additional UTC timestamp[ms] is redundant by design to detect tz/precision drift.
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

COLUMNS = ("time_msc", "timestamp_utc", "bid", "ask", "flags", "spread_points")


def _arrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is REQUIRED; parquet round-trip verification cannot be bypassed") from exc
    return pa, pq


def schema():
    pa, _ = _arrow()
    return pa.schema([
        pa.field("time_msc", pa.int64(), nullable=False),
        pa.field("timestamp_utc", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("bid", pa.float64(), nullable=False),
        pa.field("ask", pa.float64(), nullable=False),
        pa.field("flags", pa.int32(), nullable=False),
        pa.field("spread_points", pa.float64(), nullable=False),
    ])


def canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Reject invalid types rather than coercing corrupt times or replacing missing values."""
    required = set(COLUMNS) - {"timestamp_utc"}
    if frame.empty or not required.issubset(frame.columns):
        raise ValueError(f"Nonempty tick frame requires {sorted(required)}")
    result = frame.loc[:, ["time_msc", "bid", "ask", "flags", "spread_points"]].copy()
    if not pd.api.types.is_integer_dtype(result.time_msc.dtype):
        raise ValueError("time_msc must be a lossless integer millisecond timestamp")
    if not pd.api.types.is_integer_dtype(result["flags"].dtype):
        raise ValueError("MT5 flags must be integers")
    if result.isna().any().any():
        raise ValueError("Null values are forbidden in archived ticks")
    if not np.isfinite(result[["bid", "ask", "spread_points"]].to_numpy(dtype="float64")).all():
        raise ValueError("Non-finite tick numeric data")
    if (result.time_msc < 0).any() or not result.time_msc.is_monotonic_increasing:
        raise ValueError("Invalid or disordered time_msc")
    if (result.bid <= 0).any() or (result.ask <= result.bid).any() or (result.spread_points < 0).any():
        raise ValueError("Invalid bid, ask or spread")
    if ((result["flags"] < 0) | (result["flags"] > np.iinfo(np.int32).max)).any():
        raise ValueError("MT5 flags outside int32 range")
    result = result.astype({"time_msc":"int64", "bid":"float64", "ask":"float64",
                            "flags":"int32", "spread_points":"float64"})
    result.insert(1, "timestamp_utc", pd.to_datetime(result.time_msc, unit="ms", utc=True))
    return result.loc[:, list(COLUMNS)]


def write_verified_parquet(frame: pd.DataFrame, path: str | Path) -> None:
    """Write a temporary Parquet file and *read the bytes back*, exact column values.

    Caller owns fsync/atomic rename and manifest commit only AFTER this succeeds.
    """
    pa, pq = _arrow()
    expected = canonical_frame(frame)
    arrow_schema = schema()
    table = pa.Table.from_pandas(expected, schema=arrow_schema, preserve_index=False, safe=True)
    pq.write_table(table, path, version="2.6", compression="zstd", compression_level=3,
                   coerce_timestamps="ms", allow_truncated_timestamps=False)
    verify_parquet(path, expected=expected)


def verify_parquet(path: str | Path, *, expected: pd.DataFrame | None = None) -> dict:
    pa, pq = _arrow()
    src = Path(path)
    meta = pq.ParquetFile(src).metadata
    if meta.num_row_groups < 1 or meta.row_group(0).column(0).compression != "ZSTD":
        raise RuntimeError("Archived parquet is not ZSTD compressed")
    table = pq.ParquetFile(src).read()  # do not infer hive partition columns from parent path
    if not table.schema.equals(schema(), check_metadata=False):
        raise RuntimeError(f"Parquet schema drift: {table.schema}")
    frame = table.to_pandas()
    if frame.empty or len(frame) != meta.num_rows:
        raise RuntimeError("Parquet row count invalid")
    # Compare timestamps at their *declared Arrow precision*.  When pandas
    # materializes timestamp[ms, UTC], astype(int64) returns *milliseconds*,
    # whereas for timestamp[ns, UTC] it returns *nanoseconds*.  Dividing
    # astype(int64) by 1_000_000 unconditionally produces false failures and
    # can conceal unit errors.  Reading the timestamp from Arrow itself avoids
    # this ambiguity and preserves the timezone/type contract above.
    utc_ms = table.column("timestamp_utc").cast(pa.int64()).to_numpy(zero_copy_only=False)
    time_ms = frame["time_msc"].to_numpy(dtype="int64")
    if not np.array_equal(utc_ms, time_ms):
        raise RuntimeError("UTC millisecond timestamp does not match canonical time_msc")
    if frame.isna().any().any():
        raise RuntimeError("Parquet contains nulls")
    # Verify the loaded rows can still satisfy our strict original input contract.
    canonical_frame(frame)
    if expected is not None:
        if len(frame) != len(expected):
            raise RuntimeError("Parquet readback row count differs from source")
        for col in COLUMNS:
            if col == "timestamp_utc":
                # Exact expected UTC milliseconds, validated by Arrow's safe
                # conversion (rejects timestamps that require truncation).
                a = utc_ms
                b = pa.Array.from_pandas(expected[col],
                                         type=pa.timestamp("ms", tz="UTC"),
                                         safe=True).cast(pa.int64()).to_numpy(zero_copy_only=False)
            else:
                a = frame[col].to_numpy()
                b = expected[col].to_numpy()
            if not np.array_equal(a, b):
                raise RuntimeError(f"Parquet round-trip mismatch in column {col}")
    return {"rows":len(frame), "first_ms":int(frame.time_msc.iloc[0]),
            "last_ms":int(frame.time_msc.iloc[-1]), "schema_ok":True,
            "timestamp_precision":"milliseconds", "timezone":"UTC"}
