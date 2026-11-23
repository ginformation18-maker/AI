"""Five-day data gate never silently counts gaps or an untested day."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from mt5_agents.tick_collector import TickArchive
from mt5_agents.observation_audit import audit, parse_utc

BASE=datetime(2026,11,16,12,0,tzinfo=timezone.utc)


def schedule(n=5):
    return {"symbol":"XAUUSD","account_login":123,"broker_server":"TestBroker",
            "window_seconds":60,
            "sessions":[{"start_utc":(BASE+timedelta(days=d)).isoformat(),
                         "end_utc":(BASE+timedelta(days=d,hours=1)).isoformat()}
                        for d in range(n)]}


def test_missing_windows_and_less_than_five_sessions_never_pass(tmp_path):
    class Never:
        def account_info(self): return SimpleNamespace(login=123,server="TestBroker")
    TickArchive(tmp_path,Never(),123,"TestBroker")
    r=audit(tmp_path,schedule(5))
    assert not r['ready_for_manual_review']
    assert r['days'][0]['failures'][0]['issue']=='MISSING_WINDOW'
    assert not audit(tmp_path,schedule(3))['ready_for_manual_review']
    assert not audit(tmp_path,schedule(5),min_sessions=10)['ready_for_manual_review']


def test_naive_session_time_rejected(tmp_path):
    with pytest.raises(ValueError,match='UTC'):
        parse_utc('2026-11-16T12:00:00')


def test_cannot_lower_observation_gate(tmp_path):
    with pytest.raises(ValueError,match='five'):
        audit(tmp_path,schedule(1),min_sessions=1)
