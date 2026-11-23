"""Phase 1 audit tests. They intentionally never initialize or call MT5."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from mt5_agents.memory import EventLogger, PendingLabelQueue, QuoteBar, SignalSpec
from mt5_agents.memory.db import PersistenceError, read_connection, write_transaction
from mt5_agents.memory import db as db_module

T = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)


def signal(sig="s1", side="BUY", horizon=20):
    return SignalSpec(
        signal_id=sig, symbol="XAUUSD", side=side, entry_time_utc=T,
        entry_bid=100, entry_ask=101, stop_loss=96 if side == "BUY" else 105,
        take_profit=106 if side == "BUY" else 95,
        max_holding_bars=horizon, decision="SHADOW", model_version="test-v1",
        feature_version="test-feature-v1", features={"rsi":42.5, "bar": "test"},
    )


def bar(n=0, *, bid_o=100, bid_h=103, bid_l=98, bid_c=100, spread=1):
    start = T + timedelta(minutes=15*n)
    stop = start + timedelta(minutes=15)
    return QuoteBar(start, stop, bid_o, bid_h, bid_l, bid_c,
                    bid_o+spread, bid_h+spread, bid_l+spread, bid_c+spread)


@pytest.fixture
def memory(tmp_path):
    path = tmp_path / "audit.sqlite3"
    log = EventLogger(path)
    queue = PendingLabelQueue(path)
    return path, log, queue


def test_wal_busy_timeout_full_sync_foreign_keys(memory):
    path, _, _ = memory
    with read_connection(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "hypothetical_signals", "closed_labels", "observed_bars"} <= names


def test_atomic_idempotent_signal_and_conflict(memory):
    path, log, _ = memory
    assert log.record_signal(signal()) is True
    assert log.record_signal(signal()) is False
    with pytest.raises(ValueError, match="reused"):
        log.record_signal(signal(horizon=25))
    with read_connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hypothetical_signals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='SIGNAL'").fetchone()[0] == 1


def test_concurrent_writers_and_readers_no_lost_events(memory):
    path, log, _ = memory
    def writer(worker):
        for index in range(20):
            log.record_signal(signal(f"w{worker}-{index}"))
    def reader(_):
        for _ in range(25):
            with read_connection(path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                assert count >= 0
    with ThreadPoolExecutor(max_workers=7) as pool:
        tasks = [pool.submit(writer, i) for i in range(4)] + [pool.submit(reader, i) for i in range(3)]
        for task in tasks:
            task.result()
    with read_connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hypothetical_signals").fetchone()[0] == 80
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 80


def test_lock_timeout_is_explicit_fail_closed(memory, monkeypatch):
    path, log, _ = memory
    monkeypatch.setattr(db_module, "BUSY_TIMEOUT_MS", 75)
    lock = sqlite3.connect(path, isolation_level=None)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(PersistenceError, match="audit commit failed"):
            log.record_signal(signal())
    finally:
        lock.rollback()
        lock.close()
    with read_connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hypothetical_signals").fetchone()[0] == 0


def test_execution_intent_requires_signal_and_forbids_resend(memory):
    path, log, _ = memory
    with pytest.raises(ValueError, match="persisted signal"):
        log.record_execution_intent("missing", {"price":101}, at_utc=T)
    log.record_signal(signal())
    log.record_execution_intent("s1", {"price":101,"volume":0.01}, at_utc=T)
    with pytest.raises(RuntimeError, match="already exists"):
        log.record_execution_intent("s1", {"price":101,"volume":0.01}, at_utc=T)
    with read_connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='EXECUTION_INTENT'").fetchone()[0] == 1


def test_both_barriers_buy_is_stop_first(memory):
    _, log, queue = memory
    log.record_signal(signal())
    r = queue.process_closed_bar("s1", bar(bid_h=108,bid_l=94))
    assert (r.status,r.outcome) == ("LABELED", "SL")
    label = queue.get_label("s1")
    assert label["exit_quote"] == 96
    assert label["gross_price_move"] == -5  # entry was ASK 101
    assert label["training_eligible"] == 0
    assert "both_barriers" in label["reason"]
    assert queue.pending() == []


def test_both_barriers_sell_is_stop_first_using_ask(memory):
    _, log, queue = memory
    log.record_signal(signal(side="SELL"))
    # SELL closes at ASK. Bid high=105 => ask high=106 >= SL 105
    # Bid low=93 => ask low=94 <= TP 95
    r = queue.process_closed_bar("s1", bar(bid_h=105,bid_l=93))
    assert (r.status,r.outcome) == ("LABELED", "SL")
    label = queue.get_label("s1")
    assert label["exit_quote"] == 105
    assert label["gross_price_move"] == -5  # SELL opened BID 100


def test_vertical_barrier_closes_at_conservative_bid_and_is_idempotent(memory):
    path, log, queue = memory
    log.record_signal(signal(horizon=2))
    r1 = queue.process_closed_bar("s1", bar(0,bid_c=102))
    assert (r1.status,r1.bars_seen) == ("PENDING",1)
    assert queue.process_closed_bar("s1", bar(0,bid_c=102)).status == "DUPLICATE"
    # Mimic restart: create fresh queue from persistent database
    new_queue = PendingLabelQueue(path)
    r2 = new_queue.process_closed_bar("s1", bar(1,bid_c=102))
    assert (r2.status,r2.outcome,r2.bars_seen) == ("LABELED","TIME",2)
    label = new_queue.get_label("s1")
    assert label["exit_quote"] == 102
    assert label["provisional_y"] is None
    assert label["training_eligible"] == 0
    assert new_queue.process_closed_bar("s1", bar(2)).status == "ALREADY_RESOLVED"


def test_adverse_open_gap_is_worse_than_stop(memory):
    _, log, queue = memory
    log.record_signal(signal())
    queue.process_closed_bar("s1", bar(bid_o=94,bid_h=98,bid_l=93,bid_c=95))
    lbl = queue.get_label("s1")
    assert lbl["outcome"] == "SL"
    assert lbl["exit_quote"] == 94
    assert lbl["gross_price_move"] == -7


def test_incomplete_or_crossed_spread_bars_rejected(memory):
    _, log, queue = memory
    log.record_signal(signal())
    with pytest.raises(ValueError, match="crossed"):
        queue.process_closed_bar("s1", bar(spread=-1))
    with pytest.raises(ValueError, match="extrema"):
        queue.process_closed_bar("s1", bar(bid_h=99))
    assert len(queue.pending()) == 1


def test_missing_interval_must_be_reconciled(memory):
    _, log, queue = memory
    log.record_signal(signal(horizon=3))
    queue.process_closed_bar("s1", bar(0))
    with pytest.raises(ValueError, match="missing/out-of-order"):
        queue.process_closed_bar("s1", bar(2))
    assert queue.pending()[0]["bars_seen"] == 1


def test_failed_label_write_rolls_back_progress(memory):
    path, log, queue = memory
    log.record_signal(signal())
    # Force event ID clash at label persist time. No partial closed_labels row.
    log.record_trade_event("someone", "TRADE_TEST", T, {"x":1}, event_id="label:s1")
    with pytest.raises(PersistenceError, match="audit commit failed"):
        queue.process_closed_bar("s1", bar(bid_h=110))
    with read_connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observed_bars").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM closed_labels").fetchone()[0] == 0
        assert conn.execute("SELECT bars_seen FROM hypothetical_signals WHERE signal_id='s1'").fetchone()[0] == 0
