"""MT5 quote-tick adapter: authentic Bid/Ask, no invented spreads or missing bars.

The output is SHADOW-ONLY. It is not a slippage/commission/swap-aware execution
simulator and does not enable training labels. Times are UTC milliseconds.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any

import numpy as np
import pandas as pd

from .memory.pending_queue import QuoteBar


class TickQualityError(ValueError):
    """Historical ticks cannot support a reliable quote-bar or spread estimate."""


def _utc_ms(dt: datetime) -> int:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Use timezone-aware UTC datetimes")
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True)
class SpreadStats:
    count: int
    spread_points_p50: float
    spread_points_p95: float
    spread_points_max: float


@dataclass(frozen=True)
class TickBar:
    quote_bar: QuoteBar
    spread: SpreadStats
    tick_count: int
    max_gap_seconds: float


def normalize_ticks(raw: Any, *, start_utc: datetime, end_utc: datetime, point: float) -> pd.DataFrame:
    """Reject mixed/invalid quotes; do not sort or repair a corrupted stream."""
    lo, hi = _utc_ms(start_utc), _utc_ms(end_utc)
    if hi <= lo or not math.isfinite(point) or point <= 0:
        raise ValueError("Invalid time range or symbol point size")
    if raw is None:
        raise TickQualityError("MT5 returned None for tick history")
    ticks = pd.DataFrame(raw)
    if ticks.empty:
        raise TickQualityError("No ticks returned for requested interval")
    required = {"time_msc", "bid", "ask"}
    if not required.issubset(ticks.columns):
        raise TickQualityError(f"Missing tick columns: {required - set(ticks.columns)}")
    ticks = ticks.loc[:, [c for c in ("time_msc", "bid", "ask", "flags") if c in ticks.columns]].copy()
    for col in ("time_msc", "bid", "ask"):
        ticks[col] = pd.to_numeric(ticks[col], errors="raise")
    if not np.isfinite(ticks[["time_msc", "bid", "ask"]].to_numpy(dtype=float)).all():
        raise TickQualityError("Non-finite quote or timestamp")
    if not ticks.time_msc.is_monotonic_increasing:
        raise TickQualityError("Out-of-order tick stream; do not reorder silently")
    if ((ticks.time_msc < lo) | (ticks.time_msc > hi)).any():
        raise TickQualityError("Ticks outside requested UTC interval")
    if (ticks.bid <= 0).any() or (ticks.ask <= ticks.bid).any():
        raise TickQualityError("Invalid, zero or crossed quote")
    # Same millisecond can contain successive valid quote changes, with their order
    # defined by the source array. Keep all; NEVER use an unspecified duplicate drop.
    ticks["spread_points"] = (ticks.ask - ticks.bid) / point
    return ticks.reset_index(drop=True)


class TickDataAdapter:
    def __init__(self, mt5: Any):
        self.mt5 = mt5

    def fetch(self, symbol: str, start_utc: datetime, end_utc: datetime, *, point: float,
              max_ticks: int = 500_000) -> pd.DataFrame:
        if not symbol or _utc_ms(end_utc) <= _utc_ms(start_utc):
            raise ValueError("symbol and increasing UTC date range required")
        # COPY_TICKS_INFO captures Bid/Ask updates. For reproducible histories,
        # fetch in small chunks externally if broker limits a single request.
        raw = self.mt5.copy_ticks_range(symbol, start_utc, end_utc, self.mt5.COPY_TICKS_INFO)
        if raw is None:
            raise TickQualityError(f"copy_ticks_range unavailable for {symbol}: {self.mt5.last_error()}")
        if len(raw) > max_ticks:
            raise TickQualityError("Too many ticks for one batch; split UTC ranges")
        return normalize_ticks(raw, start_utc=start_utc, end_utc=end_utc, point=point)


def aggregate_quote_bars(ticks: pd.DataFrame, *, timeframe_seconds: int,
                         closed_until_utc: datetime, max_gap_seconds: float = 120.0,
                         max_edge_lag_seconds: float = 5.0) -> list[TickBar]:
    """Build complete Bid/Ask OHLC on UTC aligned intervals, never forward-fill.

    Strict coverage: quotes must appear near both bar boundaries and no internal
    quiet period may exceed max_gap_seconds. Insufficient coverage is an ERROR,
    not a fabricated market quote. Weekend/market-session policies are separate.
    """
    if timeframe_seconds <= 0 or not (0 < max_edge_lag_seconds <= timeframe_seconds):
        raise ValueError("Invalid timeframe or allowed edge lag")
    if max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be positive")
    if ticks.empty:
        raise TickQualityError("No tick data to aggregate")
    period_ms = timeframe_seconds * 1000
    cutoff = _utc_ms(closed_until_utc)
    frame = ticks.copy()
    if not frame.time_msc.is_monotonic_increasing:
        raise TickQualityError("Unordered tick data")
    frame["bar_start_ms"] = (frame.time_msc // period_ms) * period_ms
    bars = []
    seen_starts = []
    for start, group in frame.groupby("bar_start_ms", sort=True):
        start = int(start)
        end = start + period_ms
        if end > cutoff:
            continue  # ignore forming/current candle
        times = group.time_msc.to_numpy(dtype="int64")
        gaps_ms = np.diff(times)
        widest_gap = float(gaps_ms.max() / 1000) if len(gaps_ms) else 0.0
        first_lag = (int(times[0]) - start) / 1000
        last_lag = (end - int(times[-1])) / 1000
        if (first_lag > max_edge_lag_seconds or last_lag > max_edge_lag_seconds or
                widest_gap > max_gap_seconds):
            raise TickQualityError(
                f"Insufficient tick coverage at bar {start}: first={first_lag:.1f}s "
                f"last={last_lag:.1f}s widest_gap={widest_gap:.1f}s"
            )
        if seen_starts and start != seen_starts[-1] + period_ms:
            raise TickQualityError("Missing complete bar interval: do not interpolate")
        seen_starts.append(start)
        def px(col: str, op: str) -> float:
            series = group[col]
            return float({"open": series.iloc[0], "high": series.max(),
                          "low": series.min(), "close": series.iloc[-1]}[op])
        dt_open = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
        dt_end = datetime.fromtimestamp(end / 1000, tz=timezone.utc)
        quote = QuoteBar(dt_open, dt_end,
                         px("bid", "open"), px("bid", "high"), px("bid", "low"), px("bid", "close"),
                         px("ask", "open"), px("ask", "high"), px("ask", "low"), px("ask", "close"))
        quote.canonical()  # strict validation
        spread = group.spread_points
        stats = SpreadStats(len(group), float(spread.quantile(.50)),
                            float(spread.quantile(.95)), float(spread.max()))
        bars.append(TickBar(quote, stats, len(group), widest_gap))
    if not bars:
        raise TickQualityError("No fully closed bars with valid coverage")
    return bars
