"""Atomic persisted state for UTC daily equity baseline and per-bar deduplication."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os

ROOT = Path(__file__).resolve().parents[1]


class StateStore:
    def __init__(self, login: int, path: Path | None = None):
        self.path = path or (ROOT / "runtime" / f"state_{login}.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.login = login
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("login") != login:
                raise ValueError("State file belongs to a different MT5 account")
        else:
            self.data = {"login": login, "day": None, "start_equity": None, "processed": {}}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def baseline(self, equity: float, now: datetime | None = None) -> float:
        today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
        if self.data["day"] != today:
            if equity <= 0:
                raise ValueError("Invalid account equity")
            self.data["day"] = today
            self.data["start_equity"] = equity
            self.save()
        return float(self.data["start_equity"])

    def processed(self, symbol: str, candle_open: int) -> bool:
        return int(self.data["processed"].get(symbol, -1)) >= int(candle_open)

    def mark_processed(self, symbol: str, candle_open: int) -> None:
        self.data["processed"][symbol] = int(candle_open)
        self.save()
