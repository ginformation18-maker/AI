"""Polling runner: one decision per CLOSED candle per symbol, safe restart behaviour."""
import argparse
import logging
import time
from pathlib import Path
import MetaTrader5 as mt5

from .settings import Settings, ROOT
from .state import StateStore
from .market import MarketDataAgent, connect_mt5
from .analysis import TechnicalAnalysisAgent
from .ai_review import AIReviewAgent
from .news import NewsBlackoutAgent
from .risk import RiskManagementAgent
from .execution import ExecutionAgent
from .monitor import MonitoringAgent
from .logging_tools import audit


def run_once(cfg, state, data_agent, technical, ai, news, risk, execution, monitor):
    if (ROOT / "STOP").exists():
        raise RuntimeError("STOP file present: delete only after manual reconciliation")
    account = mt5.account_info()
    if account is None or int(account.login) != cfg.login:
        raise RuntimeError("Wrong MT5 account or broken connection")
    monitor.inspect(account)
    baseline = state.baseline(float(account.equity))
    if account.equity <= baseline * (1 - cfg.max_daily_loss_pct / 100):
        audit("DAILY_KILL_SWITCH", equity=float(account.equity), baseline=baseline)
        return
    for symbol in cfg.symbols:
        df = data_agent.candles(symbol)
        last_bar = int(df.iloc[-1]["time"])
        if state.processed(symbol, last_bar):
            continue
        # Persist before any possible order, preventing duplicate orders after restarts.
        state.mark_processed(symbol, last_bar)
        if not data_agent.recent_completed(last_bar, cfg.max_bar_age_seconds):
            audit("STALE_BAR", symbol=symbol, candle_open=last_bar)
            continue
        signal = technical.decide(symbol, df)
        audit("SIGNAL", symbol=symbol, candle_open=last_bar, side=signal.side, reason=signal.reason,
              atr=signal.atr, rsi=signal.rsi)
        if signal.side == "HOLD":
            continue
        allowed, news_reason = news.allowed()
        if not allowed:
            audit("VETO_NEWS_BLACKOUT", symbol=symbol, reason=news_reason)
            continue
        allowed, ai_reason = ai.allow(signal, df)
        audit("AI_REVIEW", symbol=symbol, allowed=allowed, reason=ai_reason)
        if not allowed:
            continue
        try:
            plan = risk.plan(signal, baseline)
            audit("RISK_APPROVED", symbol=symbol, side=plan.side, lots=plan.volume,
                  sl=plan.sl, tp=plan.tp, risk_budget=plan.risk_budget,
                  estimated_loss=plan.estimated_loss, estimated_margin=plan.margin)
            outcome = execution.send(plan)
            audit("EXECUTION", symbol=symbol, **outcome)
        except RuntimeError as exc:
            audit("TRADE_BLOCKED", symbol=symbol, reason=str(exc))
            # Anything uncertain about submission creates STOP; exit cleanly.
            if (ROOT / "STOP").exists():
                raise


def main():
    parser = argparse.ArgumentParser(description="MT5 AI trading agents demo-safe starter")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Settings.load()
    connected = False
    try:
        account = connect_mt5(cfg)
        connected = True
        logging.info("Connected to MT5 login=%s mode=%s trade_mode=%s", account.login, cfg.mode, account.trade_mode)
        audit("START", account=account.login, mode=cfg.mode, symbols=list(cfg.symbols),
              note="No strategy has been validated as profitable")
        state = StateStore(cfg.login)
        data = MarketDataAgent(cfg.timeframe)
        technical = TechnicalAnalysisAgent()
        ai = AIReviewAgent(cfg)
        news = NewsBlackoutAgent()
        risk = RiskManagementAgent(cfg)
        execution = ExecutionAgent(cfg)
        monitor = MonitoringAgent(cfg)
        while True:
            run_once(cfg, state, data, technical, ai, news, risk, execution, monitor)
            if args.once:
                break
            time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        logging.info("Shutdown requested")
    finally:
        if connected:
            mt5.shutdown()


if __name__ == "__main__":
    main()
