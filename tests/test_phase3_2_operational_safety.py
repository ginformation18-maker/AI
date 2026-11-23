"""Independent safety tests. No MT5 client and no network required."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3

import pytest

from mt5_agents.collector_resume_safety import verify_safe_resume
from mt5_agents.collector_health import check_health
from mt5_agents.parquet_preflight import run_preflight
from mt5_agents.training_gate import require_training_approval, TrainingNotApproved
from mt5_agents.memory.event_logger import EventLogger, SignalSpec
from mt5_agents.stop_loss_audit import audit_stop_exits

T = datetime(2026,11,20,12,tzinfo=timezone.utc)

def test_training_is_blocked_after_five_ten_even_1000_sessions():
    for n in [5, 10, 1000]:
        with pytest.raises(TrainingNotApproved, match="TRAINING_BLOCKED"):
            require_training_approval(observation_sessions=n)


def test_parquet_preflight_fails_closed_if_actual_writer_fails(tmp_path, monkeypatch):
    import mt5_agents.parquet_preflight as module
    def fail(*args,**kwargs):
        raise RuntimeError("no real arrow writer")
    monkeypatch.setattr(module,"write_verified_parquet",fail)
    with pytest.raises(RuntimeError,match="no real arrow"):
        run_preflight(tmp_path)


def test_resume_gate_empty_archive_allowed_but_stale_lock_refused(tmp_path):
    assert verify_safe_resume(tmp_path)["status"] == "SAFE_EMPTY_ARCHIVE"
    (tmp_path/"locks").mkdir()
    (tmp_path/"locks"/"untested.lock").write_text("pid=123")
    with pytest.raises(RuntimeError,match="locks"):
        verify_safe_resume(tmp_path)


def test_resume_gate_refuses_orphan_parquet_even_without_manifest(tmp_path):
    q=tmp_path/"parquet"/"symbol=XAUUSD";q.mkdir(parents=True)
    (q/"orphan.parquet").write_bytes(b"fake")
    with pytest.raises(RuntimeError,match="without manifest"):
        verify_safe_resume(tmp_path)


def test_resume_gate_refuses_uncommitted_temporary_file(tmp_path):
    q=tmp_path/"parquet";q.mkdir()
    (q/"ticks.parquet.123.tmp").write_bytes(b"incomplete")
    with pytest.raises(RuntimeError,match="temp"):
        verify_safe_resume(tmp_path)


def test_health_check_reports_missing_stale_and_gap(tmp_path):
    with pytest.raises(RuntimeError,match="single window"):
        check_health(tmp_path,"XAUUSD")
    p=tmp_path/"collector_health_XAUUSD.json"
    p.write_text(json.dumps({"last_commit_utc": (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
             "last_state":"ARCHIVED", "rss_bytes":100, "last_window_end_ms":12}))
    with pytest.raises(RuntimeError,match="Stale"):
        check_health(tmp_path,"XAUUSD")
    p.write_text(json.dumps({"last_commit_utc": datetime.now(timezone.utc).isoformat(),
             "last_state":"NO_TICKS_UNVERIFIED", "rss_bytes":100,"last_window_end_ms":12}))
    with pytest.raises(RuntimeError,match="NOT verified"):
        check_health(tmp_path,"XAUUSD")


class Broker:
    DEAL_ENTRY_IN=0
    DEAL_ENTRY_OUT=1
    DEAL_ENTRY_OUT_BY=3
    DEAL_REASON_SL=4
    DEAL_TYPE_BUY=0
    DEAL_TYPE_SELL=1

    def __init__(self, deals): self.deals=deals
    def account_info(self): return SimpleNamespace(login=123)
    def history_deals_get(self,start,end): return self.deals
    def symbol_info(self,symbol): return SimpleNamespace(point=0.01)


def deal(ticket,position_id, *,entry,type,reason,price,sl=0,order=111,magic=9001):
    return SimpleNamespace(ticket=ticket,position_id=position_id,entry=entry,type=type,
           reason=reason,price=price,sl=sl,order=order,magic=magic,symbol="XAUUSD",
           time_msc=int(T.timestamp()*1000)+ticket,volume=0.1)


def seed_signal(tmp_path,*,direction="BUY",stop=3000.0):
    db=tmp_path/"memory.sqlite3"
    memory=EventLogger(db)
    memory.record_signal(SignalSpec(
       signal_id="signal1",symbol="XAUUSD",side=direction,
       entry_time_utc=T,entry_bid=3001.0,entry_ask=3001.2,
       take_profit=(3015 if direction=="BUY" else 2990),
       stop_loss=(stop),max_holding_bars=5,decision="TEST",
       model_version="none",feature_version="test"))
    memory.record_execution_intent("signal1",{
         "symbol":"XAUUSD", "magic":9001, "sl":stop,"tp":3015 if direction=="BUY" else 2990,
         "type":0 if direction=="BUY" else 1,"comment":"AIabcdef"},at_utc=T)
    memory.record_trade_event("signal1","TRADE_BROKER_RESULT",T,
         {"status":"CONFIRMED_DONE","deal":100,"order":111},event_id="broker_result:signal1")
    return db, memory


def test_actual_long_stop_slippage_logged_once(tmp_path):
    db, memory=seed_signal(tmp_path)
    broker=Broker([deal(100,200,entry=0,type=0,reason=0,price=3001.2),
                   deal(101,200,entry=1,type=1,reason=4,price=2999.85,sl=3000.,order=112)])
    result=audit_stop_exits(broker,db,123,T-timedelta(hours=1),T+timedelta(hours=1))
    observation=result["stop_fill_observations"][0]
    assert observation["adverse_slippage_points"] == 15
    assert observation["status"] == "MEASURED_FROM_BROKER_STOP"
    assert len([e for e in memory.get_events("signal1") if e["event_type"]=="TRADE_STOP_LOSS_SLIPPAGE_OBSERVED"])==1
    audit_stop_exits(broker,db,123,T-timedelta(hours=1),T+timedelta(hours=1))
    assert len([e for e in memory.get_events("signal1") if e["event_type"]=="TRADE_STOP_LOSS_SLIPPAGE_OBSERVED"])==1


def test_short_stop_slippage_and_changed_stop_identified(tmp_path):
    db, _=seed_signal(tmp_path,direction="SELL",stop=3010.)
    broker=Broker([deal(100,200,entry=0,type=1,reason=0,price=3001.),
                   deal(110,200,entry=1,type=0,reason=4,price=3010.50,sl=3010.25)])
    info=audit_stop_exits(broker,db,123,T-timedelta(hours=1),T+timedelta(hours=1))
    rec=info["stop_fill_observations"][0]
    assert rec["adverse_slippage_points"]==25
    assert rec["stop_modified_from_original"] is True


def test_missing_exit_stop_is_not_reported_as_zero_slippage(tmp_path):
    db,_=seed_signal(tmp_path)
    broker=Broker([deal(100,200,entry=0,type=0,reason=0,price=3001.),
                   deal(111,200,entry=1,type=1,reason=4,price=2999.50,sl=0)])
    info=audit_stop_exits(broker,db,123,T-timedelta(hours=1),T+timedelta(hours=1))
    assert info["stop_fill_observations"][0]["status"]=="STOP_REFERENCE_UNAVAILABLE"
    assert info["stop_fill_observations"][0]["adverse_slippage_points"] is None


def test_magic_only_does_not_link_wrong_opening_deal(tmp_path):
    db,_=seed_signal(tmp_path)
    broker=Broker([deal(700,200,entry=0,type=0,reason=0,price=3001.,order=700),
                   deal(701,200,entry=1,type=1,reason=4,price=2999.,sl=3000.)])
    report=audit_stop_exits(broker,db,123,T-timedelta(hours=1),T+timedelta(hours=1))
    assert report["stop_fill_observations"]==[]
    assert report["unmatched_intents"][0]["status"]=="OPENING_DEAL_NOT_UNIQUELY_LINKED"


def test_broker_history_absent_is_hard_failure(tmp_path):
    db,_=seed_signal(tmp_path)
    broker=Broker(None)
    with pytest.raises(RuntimeError,match="returned None"):
        audit_stop_exits(broker,db,123,T-timedelta(hours=1),T+timedelta(hours=1))
