"""Account monitoring and emergency protection for this bot's open positions.

No automated liquidation: closing positions across outages can itself be unsafe.
"""
from pathlib import Path
import MetaTrader5 as mt5
from .settings import ROOT
from .logging_tools import audit


class MonitoringAgent:
    def __init__(self, cfg):
        self.cfg = cfg
        self._last_signature = None

    def inspect(self, account):
        positions = mt5.positions_get()
        if positions is None:
            raise RuntimeError("Cannot verify broker positions; no new trades")
        ours = [p for p in positions if int(getattr(p, "magic", -1)) == self.cfg.magic]
        for p in ours:
            if float(getattr(p, "sl", 0) or 0) <= 0 or float(getattr(p, "tp", 0) or 0) <= 0:
                (ROOT / "STOP").write_text("Bot-owned position missing SL/TP. Inspect broker immediately.\n", encoding="utf-8")
                audit("EMERGENCY_STOP_MISSING_SL_TP", symbol=p.symbol, ticket=p.ticket)
                raise RuntimeError("Position missing protective SL or TP; STOP activated")
        signature = tuple(sorted((str(p.symbol), int(p.ticket), round(float(p.volume), 8)) for p in ours))
        if signature != self._last_signature:
            audit("ACCOUNT_MONITOR", equity=float(account.equity), open_positions=len(positions),
                  bot_positions=len(ours), bot_position_details=signature)
            self._last_signature = signature
