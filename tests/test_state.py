from datetime import datetime, timezone
from mt5_agents.state import StateStore


def test_daily_baseline_persists_across_restarts(tmp_path):
    filename = tmp_path / "state.json"
    state = StateStore(123, filename)
    now = datetime(2026, 11, 21, 11, tzinfo=timezone.utc)
    assert state.baseline(10000, now) == 10000
    state.mark_processed("XAUUSD", 900)
    reloaded = StateStore(123, filename)
    assert reloaded.baseline(8500, now) == 10000
    assert reloaded.processed("XAUUSD", 900)
    assert reloaded.processed("XAUUSD", 899)
    assert not reloaded.processed("XAUUSD", 901)
    tomorrow = datetime(2026, 11, 22, 11, tzinfo=timezone.utc)
    assert reloaded.baseline(8300, tomorrow) == 8300
