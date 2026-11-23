"""Phase 2: zero real orders; all broker responses are deterministic fakes."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import importlib
import sys

import pytest

from mt5_agents.broker_reconciliation import (
    BrokerReconciler, ReconciliationRequired, correlation_comment,
    inspect_unsettled_intents, require_settled_before_new_order,
)
from mt5_agents.memory import EventLogger, SignalSpec

NOW = datetime(2026, 11, 21, 10, 10, tzinfo=timezone.utc)


class Broker:
    def __init__(self, *, login=123, positions=(), orders=(), history=(), deals=(), failed=None):
        self.login = login
        self.positions = positions
        self.orders = orders
        self.history = history
        self.deals = deals
        self.failed = failed

    def account_info(self): return SimpleNamespace(login=self.login)
    def positions_get(self): return self.positions
    def orders_get(self): return self.orders
    def history_orders_get(self, start, end):
        assert start.tzinfo and end.tzinfo and start < end
        return self.history
    def history_deals_get(self, start, end): return self.deals


def row(ticket=1, order=1, comment="", magic=777, symbol="XAUUSD", position_id=0):
    return SimpleNamespace(ticket=ticket, order=order, symbol=symbol, magic=magic,
                           comment=comment, position_id=position_id, volume=0.01)


def fixture_intent(tmp_path):
    log = EventLogger(tmp_path / "events.sqlite3")
    sig = SignalSpec(
        signal_id="s42", symbol="XAUUSD", side="BUY", entry_time_utc=NOW,
        entry_bid=3000, entry_ask=3000.20, take_profit=3010, stop_loss=2990,
        max_holding_bars=20, decision="APPROVED", model_version="test-v1",
        feature_version="features-v1", features={"atr": 3},
    )
    log.record_signal(sig)
    req = {"symbol":"XAUUSD", "magic":777, "volume":0.01,
           "comment":correlation_comment("s42", 123)}
    log.record_execution_intent("s42", req, at_utc=NOW)
    return log, req


def test_comment_is_short_and_deterministic():
    a = correlation_comment("unique-signal", 123)
    assert a.startswith("AI") and len(a) <= 30
    assert a == correlation_comment("unique-signal", 123)
    assert a != correlation_comment("unique-signal", 124)


def test_unconfirmed_intent_stays_on_hold_even_if_no_broker_activity(tmp_path):
    log, req = fixture_intent(tmp_path)
    broker = Broker()
    obs = inspect_unsettled_intents(log, broker, 123)
    assert obs[0].status == "NO_MATCH_STILL_UNCERTAIN"
    assert len(log.unsettled_execution_intents()) == 1
    assert any(ev["event_type"] == "TRADE_RECON_OBSERVATION" for ev in log.get_events("s42"))
    with pytest.raises(ReconciliationRequired):
        require_settled_before_new_order(log, broker, 123)


def test_exact_magic_comment_symbol_and_positions_are_recognized(tmp_path):
    log, req = fixture_intent(tmp_path)
    broker = Broker(positions=[row(ticket=100, comment=req["comment"])],
                    history=[row(ticket=200, comment=req["comment"])],
                    deals=[row(ticket=301, order=200, comment=req["comment"])])
    obs = inspect_unsettled_intents(log, broker, 123)[0]
    assert obs.status == "BROKER_ACTIVITY_FOUND_MANUAL_REVIEW"
    assert {entry["kind"] for entry in obs.matched_objects} == {"position", "historical_order", "deal"}
    with pytest.raises(ReconciliationRequired):
        require_settled_before_new_order(log)


def test_magic_alone_is_not_safe_identification(tmp_path):
    log, req = fixture_intent(tmp_path)
    broker = Broker(positions=[row(comment="broker-changed-comment")])
    obs = inspect_unsettled_intents(log, broker, 123)[0]
    assert obs.status == "WEAK_MAGIC_ONLY_MANUAL_REVIEW"


def test_multiple_unique_orders_require_review(tmp_path):
    log, req = fixture_intent(tmp_path)
    broker = Broker(history=[row(ticket=200, comment=req["comment"]),
                             row(ticket=201, comment=req["comment"])])
    obs = inspect_unsettled_intents(log, broker, 123)[0]
    assert obs.status == "MULTIPLE_MATCHES_MANUAL_REVIEW"


def test_broker_query_failure_never_releases_hold(tmp_path):
    log, _ = fixture_intent(tmp_path)
    broker = Broker(positions=None)
    assert inspect_unsettled_intents(log, broker, 123)[0].status == "QUERY_FAILED"
    assert log.unsettled_execution_intents()


def test_different_broker_account_never_releases_hold(tmp_path):
    log, _ = fixture_intent(tmp_path)
    assert inspect_unsettled_intents(log, Broker(login=999), 123)[0].status == "ACCOUNT_MISMATCH"


def test_old_intent_needs_manual_history_review(tmp_path):
    log, req = fixture_intent(tmp_path)
    obs = BrokerReconciler(Broker(), 123).inspect(signal_id="s42", request=req,
                                                 intent_time=NOW.isoformat(),
                                                 as_of=NOW+timedelta(days=40))
    assert obs.status == "HISTORY_WINDOW_UNSAFE"


def test_only_durable_broker_done_plus_protection_releases_unsettled_gate(tmp_path):
    log, _ = fixture_intent(tmp_path)
    log.record_trade_event("s42", "TRADE_BROKER_RESULT", NOW,
                           {"status":"CONFIRMED_DONE", "retcode":10009,
                            "deal":1001,"order":5001}, event_id="broker_result:s42")
    assert log.unsettled_execution_intents()  # receipt alone is not enough
    log.record_trade_event("s42", "TRADE_SERVER_PROTECTION_VERIFIED", NOW,
                           {"ticket":5001, "sl":2980, "tp":3020, "volume":.01},
                           event_id="protection:s42")
    assert log.unsettled_execution_intents() == []
    require_settled_before_new_order(log)


@pytest.mark.parametrize("status", ["UNKNOWN", "UNKNOWN_EXCEPTION", "NOT_CONFIRMED", "CONFIRMED_DONE"])
def test_unknown_or_ticketless_status_never_releases_hold(tmp_path, status):
    log, _ = fixture_intent(tmp_path)
    log.record_trade_event("s42", "TRADE_BROKER_RESULT", NOW,
                           {"status":status,"retcode":10009,"deal":0,"order":0},
                           event_id="broker_result:s42")
    assert log.unsettled_execution_intents()
