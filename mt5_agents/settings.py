"""Validated runtime configuration. Real-money mode is double-locked."""
from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _get_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    login: int
    password: str
    server: str
    terminal_path: str
    mode: str
    symbols: tuple[str, ...]
    timeframe: str
    poll_seconds: int
    risk_pct: float
    max_daily_loss_pct: float
    max_open_positions: int
    max_spread_atr_ratio: float
    max_quote_age_seconds: int
    max_bar_age_seconds: int
    stop_atr_multiple: float
    tp_atr_multiple: float
    margin_fraction_limit: float
    magic: int
    ai_enabled: bool
    ai_model: str
    live_enable_text: str

    @classmethod
    def load(cls) -> "Settings":
        login = os.getenv("MT5_LOGIN", "").strip()
        password = os.getenv("MT5_PASSWORD", "").strip()
        server = os.getenv("MT5_SERVER", "").strip()
        if not login.isdecimal() or not password or not server:
            raise ValueError("Specify MT5_LOGIN, MT5_PASSWORD, MT5_SERVER in .env")
        cfg = cls(
            login=int(login), password=password, server=server,
            terminal_path=os.getenv("MT5_TERMINAL_PATH", "").strip(),
            mode=os.getenv("MODE", "DRY_RUN").upper().strip(),
            symbols=tuple(s.strip() for s in os.getenv("SYMBOLS", "XAUUSD,EURUSD").split(",") if s.strip()),
            timeframe=os.getenv("TIMEFRAME", "M15").upper().strip(),
            poll_seconds=int(os.getenv("POLL_SECONDS", "15")),
            risk_pct=_get_float("RISK_PER_TRADE_PCT", 0.25),
            max_daily_loss_pct=_get_float("MAX_DAILY_LOSS_PCT", 1),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "2")),
            max_spread_atr_ratio=_get_float("MAX_SPREAD_ATR_RATIO", 0.10),
            max_quote_age_seconds=int(os.getenv("MAX_QUOTE_AGE_SECONDS", "120")),
            max_bar_age_seconds=int(os.getenv("MAX_BAR_AGE_SECONDS", "120")),
            stop_atr_multiple=_get_float("STOP_ATR_MULTIPLE", 1.5),
            tp_atr_multiple=_get_float("TAKE_PROFIT_ATR_MULTIPLE", 2.25),
            margin_fraction_limit=_get_float("MARGIN_FRACTION_LIMIT", 0.20),
            magic=int(os.getenv("MAGIC", "20261121")),
            ai_enabled=os.getenv("AI_VETO_ENABLED", "false").lower() == "true",
            ai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
            live_enable_text=os.getenv("LIVE_ENABLE_TEXT", ""),
        )
        if cfg.mode not in {"DRY_RUN", "DEMO", "LIVE"}:
            raise ValueError("MODE must be DRY_RUN, DEMO or LIVE")
        if not cfg.symbols or len(cfg.symbols) != len(set(cfg.symbols)):
            raise ValueError("SYMBOLS must be a nonempty comma-separated set")
        if cfg.timeframe not in {"M15", "H1"}:
            raise ValueError("Only M15 and H1 are supported in this starter")
        if not (0 < cfg.risk_pct <= 0.5):
            raise ValueError("For this starter, risk must be >0 and <=0.5 percent")
        if not (0 < cfg.max_daily_loss_pct <= 2):
            raise ValueError("For this starter, daily loss limit must be >0 and <=2 percent")
        if cfg.max_open_positions not in (1, 2, 3):
            raise ValueError("MAX_OPEN_POSITIONS must be between 1 and 3")
        if not (0 < cfg.max_spread_atr_ratio <= 0.2):
            raise ValueError("Spread/ATR fraction must be >0 and <=0.2")
        if not (10 <= cfg.poll_seconds <= 300):
            raise ValueError("POLL_SECONDS must be 10..300")
        if not (10 <= cfg.max_quote_age_seconds <= 300) or not (10 <= cfg.max_bar_age_seconds <= 300):
            raise ValueError("Age thresholds must be 10..300 seconds")
        if not (1 <= cfg.stop_atr_multiple <= 5 and 1 <= cfg.tp_atr_multiple <= 10):
            raise ValueError("ATR multiples out of expected range")
        if not (0 < cfg.margin_fraction_limit <= 0.25):
            raise ValueError("MARGIN_FRACTION_LIMIT must be <=0.25")
        if cfg.ai_enabled and not os.getenv("OPENAI_API_KEY"):
            raise ValueError("AI_VETO_ENABLED=true requires OPENAI_API_KEY")
        if cfg.mode == "LIVE":
            if cfg.live_enable_text != "YES_I_ACCEPT_REAL_MONEY_RISK":
                raise ValueError("LIVE mode requires deliberate text authorization in .env")
            token_file = ROOT / "ENABLE_REAL_TRADING.txt"
            if not token_file.exists() or token_file.read_text(encoding="utf-8").strip() != "I_ACCEPT_REAL_MONEY_RISK":
                raise ValueError("LIVE also requires ENABLE_REAL_TRADING.txt with exact authorization phrase")
        return cfg
