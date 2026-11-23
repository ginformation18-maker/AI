"""Phase 3 cooldown: deterministic broker fakes, absolutely no orders sent."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mt5_agents.broker_reconciliation import (
    BrokerReconciler, ReconciliationRequired, correlation_comment,
    require_settled_before_new_order,
)
from mt5_agents.memory import EventLogger, SignalSpec

T = datetime(2026, 11, 21, 10, 10, tzinfo=timezone.utc)


class BrokerWithQueryCounter:
    def __init__(self):
        self.query_count = 0

    def account_info(self):
        self.query_count += 1
        return SimpleNamespace(login=123)

    def positions_get(self):
        self.query_count += 1
        return ()

    def orders_get(self):
        self.query_count += 1
        return ()

    def history_orders_get(self, *_):
        self.query_count += 1
        return ()

    def history_deals_get(self, *_):
        self.query_count += 1
        return ()


def request():
    return {"symbol": "XAUUSD", "magic": 777,
            "comment": correlation_comment("intent-3", 123)}


def test_cooldown_does_not_query_broker_or_infer_absence():
    broker = BrokerWithQueryCounter()
    recon = BrokerReconciler(broker, 123)
    for seconds in (0, 3, 30, 59.999):
        obs = recon.inspect(signal_id="intent-3", intent_time=T.isoformat(),
                            request=request(), as_of=T + timedelta(seconds=seconds))
        assert obs.status == "COOLDOWN_PENDING"
        assert "60 seconds" in obs.details
    assert broker.query_count == 0
    assert recon.inspect(signal_id="intent-3", intent_time=T.isoformat(),
                         request=request(), as_of=T + timedelta(seconds=60)).status == "NO_MATCH_STILL_UNCERTAIN"
    assert broker.query_count == 5


def test_clock_skew_fails_closed():
    broker = BrokerWithQueryCounter()
    obs = BrokerReconciler(broker, 123).inspect(signal_id="intent-3", intent_time=T.isoformat(),
                    request=request(), as_of=T - timedelta(seconds=1))
    assert obs.status == "CLOCK_UNSAFE" and broker.query_count == 0


def test_cooldown_never_unlocks_missing_ack_after_wait(tmp_path):
    memory = EventLogger(tmp_path / "memory.sqlite3")
    memory.record_signal(SignalSpec(
        signal_id="intent-3", symbol="XAUUSD", side="BUY", entry_time_utc=T,
        entry_bid=3000, entry_ask=3000.2, take_profit=3010, stop_loss=2990,
        max_holding_bars=10, decision="YES", model_version="v3", feature_version="t3",
    ))
    memory.record_execution_intent("intent-3", request(), at_utc=T)
    broker = BrokerWithQueryCounter()
    with pytest.raises(ReconciliationRequired):
        require_settled_before_new_order(memory, broker, 123)
    assert len(memory.unsettled_execution_intents()) == 1
