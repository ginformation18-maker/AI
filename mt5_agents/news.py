"""User-maintained UTC news blackout intervals; not a live economic calendar feed."""
from datetime import datetime, timezone
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NewsBlackoutAgent:
    def __init__(self, path: Path | None = None):
        self.path = path or (ROOT / "news_blackouts.json")

    def allowed(self, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        windows = json.loads(self.path.read_text(encoding="utf-8"))
        for window in windows:
            start = datetime.fromisoformat(window["start_utc"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(window["end_utc"].replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("Invalid timezone-aware news window")
            if start <= now <= end:
                return False, window.get("reason", "Scheduled news blackout")
        return True, "No configured news blackout (NOT a live news check)"
