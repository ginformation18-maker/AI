"""Illustrative *bar-based* backtest, not a substitute for broker tick-level testing.

Entry at next bar, approximate historical bar spread, pessimistic same-bar SL priority.
No commissions, swaps, slippage, margin calls or intra-bar bid/ask path reconstruction.
Results are in R (risk multiples), not a guaranteed P&L or expected returns.
"""
import argparse
from pathlib import Path
import pandas as pd
import MetaTrader5 as mt5

from .settings import Settings, ROOT
from .market import connect_mt5, TIMEFRAMES
from .analysis import TechnicalAnalysisAgent, indicators


def backtest(symbol: str, bars: int, tf: str, max_hold: int = 24) -> pd.DataFrame:
    frame_code, _ = TIMEFRAMES[tf]
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"No broker symbol {symbol}. Check broker suffix")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError("symbol_select failed")
    rates = mt5.copy_rates_from_pos(symbol, frame_code, 0, bars)
    if rates is None or len(rates) < 150:
        raise RuntimeError("Not enough broker candles to backtest")
    df = pd.DataFrame(rates).sort_values("time").reset_index(drop=True).iloc[:-1].copy()
    df = indicators(df)
    strategy = TechnicalAnalysisAgent()
    point = float(info.point)
    trades = []
    i = 100
    while i + 1 < len(df):
        signal = strategy.decide(symbol, df.iloc[:i + 1])
        if signal.side == "HOLD" or signal.atr <= 0:
            i += 1
            continue
        j = i + 1  # NEXT candle, after signal candle has closed
        opening = df.iloc[j]
        spread = max(0, float(opening.get("spread", 0))) * point
        buy = signal.side == "BUY"
        entry = float(opening["open"]) + spread if buy else float(opening["open"])
        sl = entry - 1.5 * signal.atr if buy else entry + 1.5 * signal.atr
        tp = entry + 2.25 * signal.atr if buy else entry - 2.25 * signal.atr
        if sl <= 0 or tp <= 0:
            i += 1
            continue
        end = min(j + max_hold - 1, len(df) - 1)
        exit_price, exit_idx, exit_reason = None, end, "TIME_EXIT"
        for k in range(j, end + 1):
            bar = df.iloc[k]
            dynamic_spread = max(0, float(bar.get("spread", 0))) * point
            # MT5 bars are commonly bid OHLC: SELL exits approximate ASK = BID+spread.
            hi = float(bar["high"]) + (dynamic_spread if not buy else 0)
            lo = float(bar["low"]) + (dynamic_spread if not buy else 0)
            hit_stop = lo <= sl if buy else hi >= sl
            hit_take = hi >= tp if buy else lo <= tp
            # Unknowable within one OHLC bar: count stop first (conservative).
            if hit_stop:
                exit_price, exit_idx, exit_reason = sl, k, "STOP"
                break
            if hit_take:
                exit_price, exit_idx, exit_reason = tp, k, "TARGET"
                break
        if exit_price is None:
            final = df.iloc[end]
            exit_spread = max(0, float(final.get("spread", 0))) * point
            exit_price = float(final["close"]) + (exit_spread if not buy else 0)
        initial_risk = abs(entry - sl)
        r_value = (exit_price - entry) / initial_risk * (1 if buy else -1)
        trades.append({
            "symbol": symbol, "side": signal.side,
            "signal_utc": pd.to_datetime(int(df.iloc[i]["time"]), unit="s", utc=True).isoformat(),
            "entry_utc": pd.to_datetime(int(df.iloc[j]["time"]), unit="s", utc=True).isoformat(),
            "exit_utc": pd.to_datetime(int(df.iloc[exit_idx]["time"]), unit="s", utc=True).isoformat(),
            "entry": round(entry, int(info.digits)), "sl": round(sl, int(info.digits)),
            "tp": round(tp, int(info.digits)), "exit": round(exit_price, int(info.digits)),
            "exit_reason": exit_reason, "R_gross_no_fees": round(r_value, 4),
        })
        i = exit_idx + 1
    return pd.DataFrame(trades)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--bars", type=int, default=3000)
    parser.add_argument("--max-hold", type=int, default=24)
    args = parser.parse_args()
    if not (150 <= args.bars <= 100_000 and 1 <= args.max_hold <= 500):
        raise ValueError("Invalid bars or max-hold")
    cfg = Settings.load()
    if cfg.mode != "DRY_RUN":
        raise RuntimeError("Set MODE=DRY_RUN to run research backtest")
    try:
        connect_mt5(cfg)
        report = backtest(args.symbol, args.bars, cfg.timeframe, args.max_hold)
        out = ROOT / "runtime" / f"backtest_{args.symbol}.csv"
        out.parent.mkdir(exist_ok=True)
        report.to_csv(out, index=False, encoding="utf-8-sig")
        if report.empty:
            print(f"No signals in period. Saved {out}")
            return
        r = report["R_gross_no_fees"]
        curve = r.cumsum()
        max_dd = float((curve.cummax().clip(lower=0) - curve).max())
        print(f"Trades: {len(r)} | Win rate: {(r > 0).mean():.1%} | "
              f"Total gross R: {r.sum():.2f} | Average gross R: {r.mean():.3f} | "
              f"Max drawdown in R units: {max_dd:.2f} | Saved {out}")
        print("Warning: illustrative OHLC simulation; costs, slippage, gap fills and spread spikes NOT fully modeled.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
