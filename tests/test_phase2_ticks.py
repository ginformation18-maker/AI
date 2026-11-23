"""Quote history never substitutes synthetic spreads for broker Bid/Ask."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from mt5_agents.tick_data import (
    TickDataAdapter, TickQualityError, aggregate_quote_bars, normalize_ticks,
)

START = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=15)


def rows(times, spreads):
    dtype=[('time_msc', 'i8'), ('bid', 'f8'), ('ask', 'f8'), ('flags', 'i4')]
    vals=[]
    for t, spread in zip(times, spreads):
        bid=3000 + t * 0.01
        vals.append((int(START.timestamp()*1000) + t*1000, bid, bid+spread, 6))
    return np.array(vals, dtype=dtype)


def test_real_bid_ask_spread_points_not_constant():
    df=normalize_ticks(rows([0,1,2,3,4], [.15,.8,.2,.6,.15]),
                       start_utc=START,end_utc=END,point=.01)
    bars=aggregate_quote_bars(df,timeframe_seconds=5,closed_until_utc=END,
                              max_gap_seconds=2,max_edge_lag_seconds=1)
    assert len(bars)==1
    assert bars[0].spread.spread_points_max==pytest.approx(80)
    assert bars[0].spread.spread_points_p50==pytest.approx(20)
    assert bars[0].quote_bar.ask_high > bars[0].quote_bar.bid_high
    assert bars[0].tick_count==5


def test_rejects_missed_boundary_and_missing_bars():
    df=normalize_ticks(rows([0,1,2,3,4,10,11,12,13,14], [.15]*10),
                       start_utc=START,end_utc=END,point=.01)
    with pytest.raises(TickQualityError,match='Missing complete bar'):
        aggregate_quote_bars(df,timeframe_seconds=5,closed_until_utc=END,
                             max_gap_seconds=2,max_edge_lag_seconds=1)


def test_rejects_quotes_with_sparse_tick_coverage():
    df=normalize_ticks(rows([0,2,4], [.15]*3),start_utc=START,end_utc=END,point=.01)
    with pytest.raises(TickQualityError,match='Insufficient tick coverage'):
        aggregate_quote_bars(df,timeframe_seconds=5,closed_until_utc=END,
                             max_gap_seconds=1,max_edge_lag_seconds=1)


def test_does_not_aggregate_current_incomplete_bar():
    df=normalize_ticks(rows([0,1,2,3,4,5,6], [.15]*7),
                       start_utc=START,end_utc=END,point=.01)
    bars=aggregate_quote_bars(df,timeframe_seconds=5,
                              closed_until_utc=START+timedelta(seconds=7),
                              max_gap_seconds=2,max_edge_lag_seconds=1)
    assert len(bars)==1


def test_zero_crossed_quote_rejected():
    raw=rows([0,1],[.15,.2]); raw[1]['ask'] = raw[1]['bid'] - .01
    with pytest.raises(TickQualityError, match='crossed'):
        normalize_ticks(raw,start_utc=START,end_utc=END,point=.01)


def test_out_of_order_data_rejected():
    with pytest.raises(TickQualityError,match='Out-of-order'):
        normalize_ticks(rows([2,0,1],[.15]*3),start_utc=START,end_utc=END,point=.01)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError,match='timezone-aware'):
        normalize_ticks(rows([0],[.15]),start_utc=datetime(2026,11,20,12),
                        end_utc=END,point=.01)


def test_data_adapter_passes_utc_and_info_flag():
    class FakeMT5:
        COPY_TICKS_INFO=1
        def copy_ticks_range(self, symbol, start, end, flags):
            assert (symbol,flags)==('XAUUSD',1)
            assert start.tzinfo and end.tzinfo
            return rows([0,1,2,3,4],[.15]*5)
        def last_error(self): return 'unknown'
    df=TickDataAdapter(FakeMT5()).fetch('XAUUSD', START, END, point=.01)
    assert len(df)==5
