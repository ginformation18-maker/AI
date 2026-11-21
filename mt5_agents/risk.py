"""Hard risk rules: no ML agent may bypass these rules."""
from dataclasses import dataclass
import math
import time
import MetaTrader5 as mt5

from .math_helpers import floor_volume, price_to_tick
from .settings import MAX_DAILY_LOSS_PCT, MAX_OPEN_POSITIONS, MAX_RISK_PER_TRADE_PCT


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: str
    volume: float
    price: float
    sl: float
    tp: float
    atr: float
    risk_budget: float
    estimated_loss: float
    margin: float
    start_equity: float


class RiskManagementAgent:
    def __init__(self, cfg):
        self.cfg = cfg

    def plan(self, signal, start_equity: float) -> TradePlan:
        cfg = self.cfg
        # Revalidate immutable ceilings at the point of use; a manually built or
        # mutated configuration must never bypass the settings loader.
        if not (0 < float(cfg.risk_pct) <= MAX_RISK_PER_TRADE_PCT):
            raise RuntimeError("Configured per-trade risk exceeds the hard 0.25 percent ceiling")
        if not (0 < float(cfg.max_daily_loss_pct) <= MAX_DAILY_LOSS_PCT):
            raise RuntimeError("Configured daily loss limit exceeds the hard 1.0 percent ceiling")
        if not (1 <= int(cfg.max_open_positions) <= MAX_OPEN_POSITIONS):
            raise RuntimeError("Configured maximum positions exceeds the hard limit of 2")
        if not math.isfinite(float(start_equity)) or start_equity <= 0:
            raise RuntimeError("Daily starting equity is invalid")
        account = mt5.account_info()
        if account is None or int(account.login) != cfg.login or not math.isfinite(float(account.equity)) or account.equity <= 0:
            raise RuntimeError("Account identity unavailable or equity invalid")
        # Apply the hard ceiling directly, independent of cfg, as a circuit breaker.
        if account.equity <= start_equity * (1 - MAX_DAILY_LOSS_PCT / 100):
            raise RuntimeError("Daily drawdown kill limit reached: no new positions")
        positions = mt5.positions_get()
        if positions is None:
            raise RuntimeError("Cannot read current MT5 positions")
        if len(positions) >= MAX_OPEN_POSITIONS or len(positions) >= int(cfg.max_open_positions):
            raise RuntimeError("Maximum open positions reached (account-wide)")
        symbol_positions = mt5.positions_get(symbol=signal.symbol)
        pending = mt5.orders_get(symbol=signal.symbol)
        if symbol_positions is None or pending is None:
            raise RuntimeError("Could not verify same-symbol positions and pending orders")
        if symbol_positions or pending:
            raise RuntimeError("A position or pending order already exists on this symbol")
        info = mt5.symbol_info(signal.symbol)
        tick = mt5.symbol_info_tick(signal.symbol)
        if info is None or tick is None or tick.ask <= tick.bid or tick.bid <= 0:
            raise RuntimeError("Missing or invalid market quote")
        if time.time() - float(tick.time) > cfg.max_quote_age_seconds or float(tick.time) > time.time() + 5:
            raise RuntimeError("Quote is stale or has an invalid timestamp")
        if signal.atr <= 0 or (tick.ask - tick.bid) / signal.atr > cfg.max_spread_atr_ratio:
            raise RuntimeError("Spread is too wide relative to ATR")
        if signal.side not in {"BUY", "SELL"}:
            raise ValueError("Only trade actionable signals")
        is_buy = signal.side == "BUY"
        price = float(tick.ask if is_buy else tick.bid)
        tick_size = float(info.trade_tick_size or info.point)
        stop_distance = cfg.stop_atr_multiple * signal.atr
        tp_distance = cfg.tp_atr_multiple * signal.atr
        if is_buy:
            sl = price_to_tick(price - stop_distance, tick_size, "down", info.digits)
            tp = price_to_tick(price + tp_distance, tick_size, "up", info.digits)
        else:
            sl = price_to_tick(price + stop_distance, tick_size, "up", info.digits)
            tp = price_to_tick(price - tp_distance, tick_size, "down", info.digits)
        if sl <= 0 or tp <= 0:
            raise RuntimeError("Stop or target is invalid")
        broker_stop = float(info.trade_stops_level) * float(info.point)
        # Existing BUY exits close on BID and SELL exits close on ASK.
        if is_buy and not (sl < tick.bid - broker_stop and tp > tick.bid + broker_stop):
            raise RuntimeError("BUY stop/target violates broker stop-level constraints")
        if not is_buy and not (sl > tick.ask + broker_stop and tp < tick.ask - broker_stop):
            raise RuntimeError("SELL stop/target violates broker stop-level constraints")
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        min_lot = float(info.volume_min)
        max_lot = float(info.volume_max)
        step = float(info.volume_step)
        one_min_lot_loss = mt5.order_calc_profit(order_type, signal.symbol, min_lot, price, sl)
        if one_min_lot_loss is None or not math.isfinite(one_min_lot_loss) or one_min_lot_loss >= 0:
            raise RuntimeError(f"Cannot calculate stop-loss value: {mt5.last_error()}")
        # Risk percentage is capped in code even if supplied cfg bypassed the loader.
        risk_pct = min(float(cfg.risk_pct), MAX_RISK_PER_TRADE_PCT)
        risk_budget = min(float(account.equity), start_equity) * risk_pct / 100
        raw = risk_budget / abs(one_min_lot_loss) * min_lot
        volume = floor_volume(raw, min_lot, max_lot, step)
        if volume == 0:
            raise RuntimeError("Minimum broker lot exceeds permitted per-trade risk")
        estimated_loss = mt5.order_calc_profit(order_type, signal.symbol, volume, price, sl)
        if estimated_loss is None or not math.isfinite(estimated_loss) or estimated_loss >= 0:
            raise RuntimeError("Unable to verify loss for selected order volume")
        if abs(estimated_loss) > risk_budget + 1e-7:
            raise RuntimeError("Final volume exceeds the configured risk budget")
        margin = mt5.order_calc_margin(order_type, signal.symbol, volume, price)
        if margin is None or not math.isfinite(margin) or margin <= 0:
            raise RuntimeError("Cannot determine trade margin")
        if margin > float(account.margin_free) * cfg.margin_fraction_limit:
            raise RuntimeError("Required margin is too large for free-margin safety budget")
        return TradePlan(signal.symbol, signal.side, volume, price, sl, tp, signal.atr,
                         risk_budget, abs(estimated_loss), float(margin), start_equity)
