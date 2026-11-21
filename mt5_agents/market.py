"""Market data gateway. Never use the forming (index 0) MT5 candle."""
import time
import pandas as pd
import MetaTrader5 as mt5

TIMEFRAMES = {"M15": (mt5.TIMEFRAME_M15, 15 * 60), "H1": (mt5.TIMEFRAME_H1, 60 * 60)}


class MarketDataAgent:
    def __init__(self, timeframe: str):
        self.mt5_timeframe, self.seconds = TIMEFRAMES[timeframe]

    def candles(self, symbol: str, count: int = 300) -> pd.DataFrame:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"Unknown broker symbol {symbol}; check Market Watch suffix")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select failed: {symbol}: {mt5.last_error()}")
        rates = mt5.copy_rates_from_pos(symbol, self.mt5_timeframe, 0, count)
        if rates is None or len(rates) < 80:
            raise RuntimeError(f"Insufficient bars for {symbol}: {mt5.last_error()}")
        df = pd.DataFrame(rates).sort_values("time").reset_index(drop=True)
        # The most recent bar is still forming; EXCLUDE it to prevent look-ahead.
        df = df.iloc[:-1].copy()
        if not df["time"].is_unique:
            raise RuntimeError(f"Duplicate candle timestamps for {symbol}")
        return df

    def recent_completed(self, last_open: int, max_age_seconds: int) -> bool:
        seconds_since_close = time.time() - (int(last_open) + self.seconds)
        return 0 <= seconds_since_close <= max_age_seconds


def connect_mt5(cfg):
    kwargs = {"login": cfg.login, "password": cfg.password, "server": cfg.server, "timeout": 30_000}
    if cfg.terminal_path:
        ok = mt5.initialize(cfg.terminal_path, **kwargs)
    else:
        ok = mt5.initialize(**kwargs)
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None or int(account.login) != cfg.login:
        mt5.shutdown()
        raise RuntimeError("MT5 account identity mismatch")
    if cfg.mode == "DEMO" and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        raise RuntimeError("DEMO execution strictly requires an MT5 demo account")
    if cfg.mode == "LIVE" and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_REAL:
        mt5.shutdown()
        raise RuntimeError("LIVE execution requires MT5 real account and both explicit unlocks")
    return account
