"""Only this module may send live/demo orders. Never automatically retry an ambiguous result."""
import logging
import math
from datetime import datetime, timezone
import time
from pathlib import Path
import MetaTrader5 as mt5

from .settings import ROOT
from .broker_reconciliation import correlation_comment, require_settled_before_new_order

LOG = logging.getLogger(__name__)


def _verify_bracket_echo(check, request, tick_size):
    """MT5 order_check echo is necessary input validation, NOT proof of server protection."""
    echoed = getattr(check, "request", None)
    if echoed is None:
        raise RuntimeError("Broker order_check did not return request echo; reject unverified SL/TP")
    for key in ("sl", "tp"):
        actual = float(getattr(echoed, key, float("nan")))
        if not math.isfinite(actual) or actual <= 0 or abs(actual - request[key]) > tick_size / 2 + 1e-8:
            raise RuntimeError(f"Broker precheck changed/missed {key}; no naked trade permitted")


def _verify_active_server_bracket(broker, request, tick_size):
    """Post-ack read-only verification. No position => UNKNOWN, never auto re-enter.

    The MT5 terminal exposes the server-recorded position SL/TP. This is not a
    guarantee that stops will fill at the exact price (gaps/slippage are possible).
    """
    rows = broker.positions_get(symbol=request["symbol"])
    if rows is None or len(rows) != 1:
        raise RuntimeError("Cannot unambiguously identify one protected post-fill position")
    pos = rows[0]
    if (getattr(pos, "symbol", None) != request["symbol"]
            or int(getattr(pos, "magic", -1)) != int(request["magic"])
            or int(getattr(pos, "type", -1)) != int(request["type"])
            or float(getattr(pos, "volume", 0)) <= 0):
        raise RuntimeError("Post-fill position identity/side/volume did not match request")
    for key in ("sl", "tp"):
        actual = float(getattr(pos, key, float("nan")))
        if not math.isfinite(actual) or actual <= 0 or abs(actual - request[key]) > tick_size / 2 + 1e-8:
            raise RuntimeError(f"UNPROTECTED position: broker {key} missing/different; manual intervention required")
    return {"ticket": int(getattr(pos, "ticket", 0)), "sl": float(pos.sl),
            "tp": float(pos.tp), "volume": float(pos.volume)}


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

    def send(self, plan, *, memory, signal_id: str) -> dict:
        cfg = self.cfg
        if (ROOT / "STOP").exists():
            raise RuntimeError("STOP kill-switch file exists; execution is disabled")
        account = self._identity_guard()
        if cfg.mode != "DRY_RUN":
            try:
                require_settled_before_new_order(memory, mt5, cfg.login)
            except Exception:
                (ROOT / "STOP").write_text("Unresolved broker intent. Manual reconciliation required.\n", encoding="utf-8")
                raise
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
        tick_size = float(getattr(info, "trade_tick_size", 0) or info.point)
        if not math.isfinite(tick_size) or tick_size <= 0:
            raise RuntimeError("Cannot validate broker tick size for protected execution")
        order_mode = getattr(info, "order_mode", None)
        # MQL5 SYMBOL_ORDER_SL=16 and SYMBOL_ORDER_TP=32. Unknown flags -> fail closed.
        if order_mode is None or (int(order_mode) & (16 | 32)) != (16 | 32):
            raise RuntimeError("Broker symbol does not explicitly permit BOTH server SL and TP")
        if not all(math.isfinite(float(x)) and float(x) > 0 for x in (plan.sl, plan.tp)):
            raise RuntimeError("Invalid SL/TP: broker protection is mandatory")
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
            "comment": correlation_comment(signal_id, cfg.login),
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
        _verify_bracket_echo(check, request, tick_size)
        # Executing an order without a durable, unique intent is forbidden.
        # A pre-existing intent means broker reconciliation is required, NOT retry.
        try:
            memory.record_execution_intent(signal_id, request, at_utc=datetime.now(timezone.utc))
        except Exception:
            # May be a duplicate intent after a crash, or an unavailable disk.
            # Prevent any further orders until the broker has been reconciled.
            (ROOT / "STOP").write_text(
                "Could not record unique execution intent. RECONCILE before restart.\n",
                encoding="utf-8",
            )
            raise
        LOG.warning("Submitting %s order: %s", cfg.mode, request)
        try:
            result = mt5.order_send(request)
        except Exception as exc:
            # An exception after send does not prove non-execution.
            (ROOT / "STOP").write_text("order_send exception: broker state uncertain. RECONCILE.\n", encoding="utf-8")
            try:
                memory.record_trade_event(
                    trade_ref=signal_id, kind="TRADE_BROKER_RESULT",
                    at_utc=datetime.now(timezone.utc),
                    details={"status":"UNKNOWN_EXCEPTION", "error_type":type(exc).__name__},
                    event_id=f"broker_result:{signal_id}",
                )
            finally:
                raise RuntimeError("order_send raised; do not resubmit. STOP active") from exc
        details = {"status": "UNKNOWN"}
        if result is not None:
            code = int(result.retcode)
            deal = int(getattr(result, "deal", 0) or 0)
            order = int(getattr(result, "order", 0) or 0)
            # Receipt DONE with a nonzero reference is the only acknowledged
            # terminal result. Even then we must monitor actual fills thereafter.
            confirmed = code == mt5.TRADE_RETCODE_DONE and (deal > 0 or order > 0)
            details = {
                "status":"CONFIRMED_DONE" if confirmed else "NOT_CONFIRMED",
                "retcode":code, "deal":deal, "order":order,
                "volume":float(getattr(result, "volume", 0) or 0),
            }
        try:
            memory.record_trade_event(
                trade_ref=signal_id, kind="TRADE_BROKER_RESULT",
                at_utc=datetime.now(timezone.utc), details=details,
                event_id=f"broker_result:{signal_id}",
            )
        except Exception:
            (ROOT / "STOP").write_text(
                "Broker call returned but result audit failed. RECONCILE before restart.\n",
                encoding="utf-8",
            )
            raise
        if details.get("status") == "CONFIRMED_DONE":
            try:
                protected = _verify_active_server_bracket(mt5, request, tick_size)
                memory.record_trade_event(
                    trade_ref=signal_id, kind="TRADE_SERVER_PROTECTION_VERIFIED",
                    at_utc=datetime.now(timezone.utc), details=protected,
                    event_id=f"protection:{signal_id}",
                )
            except Exception as exc:
                # A filled but exposed position may exist. Never wait a minute to
                # repair it in code; stop and alert the human operator immediately.
                (ROOT / "STOP").write_text(
                    "URGENT: broker ACK but server-side SL/TP NOT verified. "
                    "Inspect and protect/close position MANUALLY in MT5. No resends.\n",
                    encoding="utf-8",
                )
                raise RuntimeError("URGENT UNVERIFIED PROTECTION: STOP active, manual broker review now") from exc
            return {**details, "status": "BROKER_ACK_DONE_PROTECTED", "protection": protected}
        # All other paths are uncertain until reconciled by a human operator.
        # In particular: NONE, PARTIAL, PLACED, TIMEOUT, CONNECTION or rejection.
        (ROOT / "STOP").write_text("Unconfirmed order outcome: manual broker reconciliation required.\n", encoding="utf-8")
        raise RuntimeError(f"Unconfirmed broker execution: {details}. STOP activated")
