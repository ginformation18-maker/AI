"""Deterministic signal agents: EMA crossover + RSI filter, ATR-based exits."""
from dataclasses import dataclass
import pandas as pd
import numpy as np


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str               # BUY, SELL or HOLD
    candle_open: int
    atr: float
    rsi: float
    reason: str


def indicators(candles: pd.DataFrame) -> pd.DataFrame:
    df = candles.copy()
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    df["ema_fast"] = close.ewm(span=20, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=50, adjust=False).mean()
    changes = close.diff()
    up = changes.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    down = (-changes.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = up / down.replace(0.0, np.nan)
    df["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)
    previous_close = close.shift(1)
    tr = pd.concat([(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return df


class TechnicalAnalysisAgent:
    def decide(self, symbol: str, df: pd.DataFrame) -> Signal:
        if len(df) < 80:
            raise ValueError("Minimum 80 closed candles required")
        if not {"ema_fast", "ema_slow", "rsi", "atr"}.issubset(df.columns):
            df = indicators(df)
        now, previous = df.iloc[-1], df.iloc[-2]
        candle = int(now["time"])
        values = [now[k] for k in ("ema_fast", "ema_slow", "rsi", "atr")]
        if not np.all(np.isfinite(values)) or now["atr"] <= 0:
            return Signal(symbol, "HOLD", candle, 0.0, 50.0, "Invalid or missing indicators")
        upward = previous["ema_fast"] <= previous["ema_slow"] and now["ema_fast"] > now["ema_slow"]
        downward = previous["ema_fast"] >= previous["ema_slow"] and now["ema_fast"] < now["ema_slow"]
        rsi = float(now["rsi"])
        if upward and 50 < rsi < 70:
            side, why = "BUY", "EMA20 crossed above EMA50 with 50<RSI<70"
        elif downward and 30 < rsi < 50:
            side, why = "SELL", "EMA20 crossed below EMA50 with 30<RSI<50"
        else:
            side, why = "HOLD", "No filtered EMA crossover"
        return Signal(symbol, side, candle, float(now["atr"]), rsi, why)
