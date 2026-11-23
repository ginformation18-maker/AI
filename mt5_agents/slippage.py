"""Research-only, pessimistic slippage scenario, NOT a calibrated execution model.

Uses a known-at-decision ATR, observed point spread, and timezone-aware liquidity
session multiplier. Spread is separately paid by crossing Bid/Ask; here spread
only increases the assumed EXTRA slippage from worse liquidity, not a duplicate fee.

No order book in most OTC CFD feeds; no claims about real market depth. News
releases require a separate trusted event feed or explicit stress scenario.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math


def session_factor(at_utc: datetime) -> tuple[str, float]:
    if not isinstance(at_utc, datetime) or at_utc.tzinfo is None or at_utc.utcoffset() is None:
        raise ValueError("Slippage requires actual timezone-aware UTC entry/exit time")
    utc = at_utc.astimezone(timezone.utc)
    ny = utc.astimezone(ZoneInfo("America/New_York"))
    london = utc.astimezone(ZoneInfo("Europe/London"))
    tokyo = utc.astimezone(ZoneInfo("Asia/Tokyo"))
    if utc.weekday() >= 5:
        # In practice gold market closures vary by broker: no pretend weekend fill.
        raise ValueError("Weekend fill time: inspect broker trading session and tick continuity")
    if 8 <= ny.hour < 11:
        return "NEW_YORK_OPEN_ASSUMED", 1.8
    if 8 <= london.hour < 10:
        return "LONDON_OPEN_ASSUMED", 1.5
    if 8 <= ny.hour < 17:
        return "NEW_YORK_SESSION_ASSUMED", 1.25
    if 9 <= tokyo.hour < 16:
        return "ASIA_SESSION_ASSUMED", 1.0
    return "OFF_PEAK_ASSUMED", 1.4


@dataclass(frozen=True)
class SlippagePenalty:
    atr_fraction: float = 0.05
    min_points: float = 1.0
    stress_multiplier: float = 1.0
    reference_spread_points: float | None = None  # must be measured from this broker/account
    spread_sensitivity: float = 1.0
    news_stress_multiplier: float = 1.0   # 1 by default; explicit high-risk scenario only

    def breakdown(self, *, atr: float, point: float, spread_points: float,
                  at_utc: datetime) -> dict:
        inputs = (atr, point, spread_points, self.atr_fraction, self.min_points,
                  self.stress_multiplier, self.spread_sensitivity, self.news_stress_multiplier)
        if (not all(math.isfinite(x) for x in inputs) or atr <= 0 or point <= 0
                or spread_points < 0 or not 0 <= self.atr_fraction <= 1
                or self.min_points < 0 or not 1 <= self.stress_multiplier <= 10
                or not 0 <= self.spread_sensitivity <= 5
                or not 1 <= self.news_stress_multiplier <= 10):
            raise ValueError("Invalid ATR / spread / slippage stress parameters")
        reference = self.reference_spread_points
        if reference is None or not math.isfinite(reference) or reference <= 0:
            raise ValueError("Provide a measured, positive reference_spread_points from the same broker/account")
        session, factor = session_factor(at_utc)
        spread_factor = 1 + max(0.0, spread_points / reference - 1.0) * self.spread_sensitivity
        base = max(atr * self.atr_fraction, self.min_points * point)
        penalty = base * spread_factor * factor * self.stress_multiplier * self.news_stress_multiplier
        if not math.isfinite(penalty):
            raise ValueError("Non-finite calculated slippage")
        return {"penalty": penalty, "session": session,
                "session_factor": factor, "spread_factor": spread_factor,
                "observed_spread_points": spread_points,
                "reference_spread_points": reference,
                "news_stress_multiplier": self.news_stress_multiplier}

    def price_penalty(self, *, atr: float, point: float, spread_points: float,
                      at_utc: datetime) -> float:
        return self.breakdown(atr=atr, point=point, spread_points=spread_points,
                              at_utc=at_utc)["penalty"]


def adverse_fill(price: float, *, side: str, is_entry: bool, penalty: float) -> float:
    """BUY entry worse higher, BUY exit worse lower; SELL is the reverse."""
    if side not in ("BUY", "SELL") or not math.isfinite(price) or price <= 0:
        raise ValueError("Invalid quote/side")
    if not math.isfinite(penalty) or penalty < 0:
        raise ValueError("Invalid penalty")
    direction = 1 if (side == "BUY") == is_entry else -1
    fill = price + direction * penalty
    if fill <= 0 or not math.isfinite(fill):
        raise ValueError("Slippage makes simulated fill invalid")
    return fill
