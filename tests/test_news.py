from datetime import datetime, timezone
import json
from mt5_agents.news import NewsBlackoutAgent


def test_blackout_window(tmp_path):
    path = tmp_path / "news.json"
    path.write_text(json.dumps([{"start_utc": "2026-11-21T12:00:00Z", "end_utc": "2026-11-21T13:00:00Z",
                                 "reason": "Illustrative event"}]), encoding="utf-8")
    ai = NewsBlackoutAgent(path)
    assert ai.allowed(datetime(2026, 11, 21, 12, 30, tzinfo=timezone.utc))[0] is False
    assert ai.allowed(datetime(2026, 11, 21, 13, 30, tzinfo=timezone.utc))[0] is True
