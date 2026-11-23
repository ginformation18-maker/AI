from types import SimpleNamespace
import numpy as np
import pandas as pd
from mt5_agents.analysis import TechnicalAnalysisAgent, indicators


def sample(ema_last, ema_prev, rsi):
    df = pd.DataFrame({
        "time": np.arange(100) * 900,
        "open": np.ones(100) * 100,
        "high": np.ones(100) * 101,
        "low": np.ones(100) * 99,
        "close": np.ones(100) * 100,
        "ema_fast": np.ones(100) * 100,
        "ema_slow": np.ones(100) * 100,
        "rsi": np.ones(100) * 50,
        "atr": np.ones(100) * 2,
    })
    df.loc[98, "ema_fast"] = ema_prev
    df.loc[99, "ema_fast"] = ema_last
    df.loc[[98, 99], "ema_slow"] = 100
    df.loc[99, "rsi"] = rsi
    return df


def test_ema_upward_cross_is_buy_only_when_rsi_valid():
    assert TechnicalAnalysisAgent().decide("XAUUSD", sample(101, 99, 60)).side == "BUY"
    assert TechnicalAnalysisAgent().decide("XAUUSD", sample(101, 99, 75)).side == "HOLD"


def test_ema_downward_cross():
    assert TechnicalAnalysisAgent().decide("EURUSD", sample(99, 101, 40)).side == "SELL"


def test_causal_indicators_no_future_data():
    close = np.linspace(100, 140, 100) + np.sin(np.arange(100))
    df = pd.DataFrame({"time": np.arange(100) * 900, "close": close,
                       "high": close + 1, "low": close - 1, "open": close})
    base = indicators(df)
    modified = df.copy()
    modified.loc[99, "close"] = 99999
    other = indicators(modified)
    for name in ("ema_fast", "ema_slow", "rsi", "atr"):
        assert np.allclose(base[name].iloc[20:99], other[name].iloc[20:99], equal_nan=True)
