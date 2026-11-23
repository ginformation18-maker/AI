"""Regression: Pandas DataFrame.flags is an API property, not the MT5 column."""
import numpy as np
import pandas as pd
import pytest

from mt5_agents.parquet_integrity import canonical_frame


def _ticks(flags):
    return pd.DataFrame({
        "time_msc": np.array([1795183200000, 1795183200001], dtype="int64"),
        "bid": [4100.0, 4100.1],
        "ask": [4100.2, 4100.3],
        "flags": flags,
        "spread_points": [20.0, 20.0],
    })


def test_dataframe_flags_column_retained():
    result = canonical_frame(_ticks(np.array([2, 6], dtype="int32")))
    assert result["flags"].dtype == np.dtype("int32")
    assert result["flags"].tolist() == [2, 6]
    assert str(result["timestamp_utc"].dt.tz) == "UTC"


def test_dataframe_flags_negative_rejected():
    with pytest.raises(ValueError, match="outside int32 range"):
        canonical_frame(_ticks(np.array([2, -1], dtype="int32")))


def test_dataframe_flags_not_integer_rejected():
    with pytest.raises(ValueError, match="flags must be integers"):
        canonical_frame(_ticks(np.array([2.0, 6.0], dtype="float64")))
