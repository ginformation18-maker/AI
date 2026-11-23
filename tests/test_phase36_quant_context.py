"""Pure offline tests; no MetaTrader5 or broker connectivity needed."""

import numpy as np
import pytest

from mt5_agents.phase36_quant_context import compute_window_context, _validate_arrays


def _good():
    # 60 seconds, exactly one valid quote update each second.
    ts = np.arange(0, 60_000, 1000, dtype='int64')
    bid = 3500 + np.arange(60) * .01
    ask = bid + .15
    spread_points = np.full(60, 15.0)
    return ts, bid, ask, spread_points


def test_constant_spread_and_velocity():
    ms, bid, ask, sp = _good()
    out = compute_window_context(ms, bid, ask, sp, start_ms=0, end_ms=60_000)
    assert out['quote_tick_count'] == 60
    assert out['spread_points_p95_window_only'] == 15
    assert out['spread_change_points_last_minus_first'] == 0
    assert out['quote_ticks_per_second_mean_over_full_window'] == 1
    assert out['quote_ticks_per_second_p95_across_one_second_buckets'] == 1
    assert out['seconds_with_quote_update'] == 60
    assert out['edge_and_gap_coverage_candidate'] is True
    assert out['is_historical_statistical_baseline'] is False
    assert out['is_tradable_signal'] is False
    assert out['timebase'] == 'BROKER_RAW_NOT_CERTIFIED_UTC'


def test_spread_distribution_and_mid_volatility():
    ms, bid, ask, sp = _good()
    sp[-1] = 25
    ask[-1] = bid[-1] + .25
    out = compute_window_context(ms, bid, ask, sp, start_ms=0, end_ms=60_000)
    assert out['spread_points_max'] == 25
    assert out['spread_change_points_last_minus_first'] == 10
    assert out['mid_quote_realized_volatility_log_returns_window'] > 0
    assert out['spread_points_p95_window_only'] == 15  # 1/60 spike is below p95


def test_gap_does_not_turn_into_fake_coverage():
    ms, bid, ask, sp = _good()
    keep = (ms < 10_000) | (ms >= 30_000)
    out = compute_window_context(ms[keep], bid[keep], ask[keep], sp[keep],
                                 start_ms=0, end_ms=60_000)
    assert out['edge_and_gap_coverage_candidate'] is False
    assert out['largest_intertick_gap_seconds'] > 10


def test_duplicate_millisecond_keeps_both_quotes():
    ms, bid, ask, sp = _good()
    ms[1] = ms[0]
    out = compute_window_context(ms, bid, ask, sp, start_ms=0, end_ms=60_000)
    assert out['quote_tick_count'] == 60
    assert out['seconds_with_quote_update'] == 59


@pytest.mark.parametrize('which', ['unordered','outside','crossed','nan','incorrect_points','empty'])
def test_invalid_data_rejected(which):
    ms, bid, ask, sp = _good()
    if which == 'unordered':
        ms[30], ms[31] = ms[31], ms[30]
    elif which == 'outside':
        ms[-1] = 60_000
    elif which == 'crossed':
        ask[20] = bid[20] - .01
    elif which == 'nan':
        bid[20] = np.nan
    elif which == 'incorrect_points':
        sp[-1] = 300.0
    elif which == 'empty':
        ms, bid, ask, sp = ms[:0], bid[:0], ask[:0], sp[:0]
    with pytest.raises(ValueError):
        compute_window_context(ms, bid, ask, sp, start_ms=0, end_ms=60_000)


def test_noninteger_timestamp_rejected():
    ms, bid, ask, sp = _good()
    with pytest.raises(ValueError):
        _validate_arrays(ms.astype(float), bid, ask, sp, 0, 60_000)


def test_invalid_window_range_rejected():
    ms, bid, ask, sp = _good()
    with pytest.raises(ValueError):
        compute_window_context(ms, bid, ask, sp, start_ms=0, end_ms=60_001)


def _archive_fixture(tmp_path):
    pytest.importorskip('pyarrow')
    import pandas as pd
    from mt5_agents.parquet_integrity import write_verified_parquet
    ms, bid, ask, sp = _good()
    root = tmp_path / 'parquet'
    folder = root / 'symbol=XAUUSD'
    folder.mkdir(parents=True)
    path = folder / 'ticks_0000000000000_0000000060000.parquet'
    data = pd.DataFrame({'time_msc': ms, 'bid': bid, 'ask': ask,
                         'flags': np.zeros(len(ms), dtype=np.int32), 'spread_points': sp})
    write_verified_parquet(data, path)
    return root, path


def test_real_parquet_one_window_has_descriptive_metrics_only(tmp_path):
    from mt5_agents.phase36_quant_context import inspect_archive_quant
    root, _ = _archive_fixture(tmp_path)
    report = inspect_archive_quant(root, 'XAUUSD')
    assert report['total_quote_ticks'] == 60
    assert report['archived_windows_read'] == 1
    assert report['ready_for_baseline'] is False
    assert report['writes_archive'] is False
    assert report['sends_orders'] is False
    assert report['window_metrics'][0]['spread_points_p95_window_only'] == 15.0
    assert len(report['window_metrics'][0]['sha256']) == 64


def test_broken_parquet_fails_closed(tmp_path):
    from mt5_agents.phase36_quant_context import inspect_archive_quant
    root, path = _archive_fixture(tmp_path)
    path.write_bytes(b'not parquet')
    with pytest.raises(Exception):
        inspect_archive_quant(root, 'XAUUSD')
