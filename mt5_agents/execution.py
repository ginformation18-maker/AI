"""Only this module may send live/demo orders. Never automatically retry an ambiguous result."""
import logging
import time
from pathlib import Path
import MetaTrader5 as mt5

from .settings import ROOT

LOG = logging.getLogger(__name__)


class ExecutionAgent:
    def __init__(self, cfg):
        self.cfg = cfg

    def _identity_guard(self):
        account = mt5.account_info()
        if account is None or int(account.login) != self.cfg.login:
            raise RuntimeError("MT5 account changed. Execution blocked.")
        if self.cfg.mode == "DEMO" and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("Refusing execution on non-demo account")
        if self.cfg.mode == "LIVE":
            auth = ROOT / "ENABLE_REAL_TRADING.txt"
            if (account.trade_mode != mt5.ACCOUNT_TRADE_MODE_REAL
                    or self.cfg.live_enable_text != "YES_I_ACCEPT_REAL_MONEY_RISK"
                    or not auth.exists() or auth.read_text(encoding="utf-8").strip() != "I_ACCEPT_REAL_MONEY_RISK"):
                raise RuntimeError("Real-money execution lock not satisfied")
        return account

    def send(self, plan) -> dict:
        cfg = self.cfg
        if (ROOT / "STOP").exists():
            raise RuntimeError("STOP kill-switch file exists; execution is disabled")
        account = self._identity_guard()
        if cfg.mode != "DRY_RUN":
            terminal = mt5.terminal_info()
            if terminal is None or not terminal.trade_allowed or not account.trade_allowed:
                raise RuntimeError("Automated trading is not allowed in MT5 terminal/account")
        positions = mt5.positions_get(symbol=plan.symbol)
        orders = mt5.orders_get(symbol=plan.symbol)
        all_positions = mt5.positions_get()
        if positions is None or orders is None or all_positions is None:
            raise RuntimeError("Cannot re-check open positions before order")
        if positions or orders or len(all_positions) >= cfg.max_open_positions:
            raise RuntimeError("Position/pending-order limit changed before execution")
        tick = mt5.symbol_info_tick(plan.symbol)
        info = mt5.symbol_info(plan.symbol)
        if tick is None or info is None or tick.ask <= tick.bid:
            raise RuntimeError("Quote unavailable during preflight")
        if time.time() - float(tick.time) > cfg.max_quote_age_seconds:
            raise RuntimeError("Quote became stale")
        new_price = float(tick.ask if plan.side == "BUY" else tick.bid)
        allowed_drift = min(20 * float(info.point), 0.03 * plan.atr)
        if abs(new_price - plan.price) > allowed_drift:
            raise RuntimeError("Price moved too far since risk sizing; wait for a new signal")
        if (tick.ask - tick.bid) / plan.atr > cfg.max_spread_atr_ratio:
            raise RuntimeError("Spread increased above safe limit")
        # Re-check minimum stops AND risk after price drift.
        stop_level = float(info.trade_stops_level) * float(info.point)
        if plan.side == "BUY":
            if not (plan.sl < tick.bid - stop_level and plan.tp > tick.bid + stop_level):
                raise RuntimeError("BUY stop/target no longer valid")
            order_type = mt5.ORDER_TYPE_BUY
        else:
            if not (plan.sl > tick.ask + stop_level and plan.tp < tick.ask - stop_level):
                raise RuntimeError("SELL stop/target no longer valid")
            order_type = mt5.ORDER_TYPE_SELL
        recalculated = mt5.order_calc_profit(order_type, plan.symbol, plan.volume, new_price, plan.sl)
        if recalculated is None or recalculated >= 0 or abs(recalculated) > plan.risk_budget + 1e-7:
            raise RuntimeError("Risk budget failed after price drift")
        # Broker reports filling_mode as flags: SYMBOL_FILLING_FOK=1, IOC=2.
        flags = int(info.filling_mode)
        if flags & 1:
            fill_policy = mt5.ORDER_FILLING_FOK
        elif flags & 2:
            fill_policy = mt5.ORDER_FILLING_IOC
        else:
            raise RuntimeError("Unsupported broker filling mode; configure manually after verification")
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": plan.symbol,
            "volume": plan.volume,
            "type": order_type,
            "price": round(new_price, int(info.digits)),
            "sl": plan.sl,
            "tp": plan.tp,
            "deviation": 20,
            "magic": cfg.magic,
            "comment": "MT5AI-gated-v1",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": fill_policy,
        }
        if cfg.mode == "DRY_RUN":
            LOG.info("DRY_RUN: order NOT sent: %s", request)
            return {"status": "DRY_RUN", "request": request}
        # Reject if daily equity has dropped since plan generation.
        account_now = mt5.account_info()
        if account_now is None or int(account_now.login) != cfg.login:
            raise RuntimeError("Account disappeared before order check")
        if float(account_now.equity) <= plan.start_equity * (1 - cfg.max_daily_loss_pct / 100):
            # Re-check the UTC-day equity baseline immediately before submission.
            raise RuntimeError("Equity fell sharply before execution")
        check = mt5.order_check(request)
        if check is None or check.retcode != 0:
            raise RuntimeError(f"MT5 order_check failed: {getattr(check, 'retcode', 'None')} {getattr(check, 'comment', '')}")
        LOG.warning("Submitting %s order: %s", cfg.mode, request)
        result = mt5.order_send(request)
        if result is None:
            (ROOT / "STOP").write_text("Ambiguous MT5 order result. Inspect broker history before restarting.\n", encoding="utf-8")
            raise RuntimeError(f"AMBIGUOUS order_send returned None: {mt5.last_error()}. STOP activated")
        # Partial fill is NOT retried: broker may have an open position already.
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"status": "FILLED", "retcode": result.retcode, "deal": int(result.deal),
                    "order": int(result.order), "volume": float(result.volume)}
        if result.retcode == mt5.TRADE_RETCODE_DONE_PARTIAL:
            (ROOT / "STOP").write_text("Partial fill: inspect open positions before restarting.\n", encoding="utf-8")
            return {"status": "PARTIAL_FILL_STOP", "retcode": result.retcode,
                    "order": int(result.order), "volume": float(result.volume)}
        if result.retcode in {mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_TIMEOUT, mt5.TRADE_RETCODE_CONNECTION}:
            (ROOT / "STOP").write_text("Uncertain order outcome: reconcile before restarting.\n", encoding="utf-8")
            raise RuntimeError(f"Uncertain broker execution: {result.retcode}. STOP activated")
        raise RuntimeError(f"Order rejected: retcode={result.retcode}; comment={result.comment}")
