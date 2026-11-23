"""Send gate integration: fake MT5 prevents actual brokerage activity."""
from datetime import datetime, timezone
from types import SimpleNamespace
import importlib
import sys
import time

import pytest

from mt5_agents.memory import EventLogger, SignalSpec
from mt5_agents.broker_reconciliation import correlation_comment, ReconciliationRequired


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ORDER_TYPE_BUY=0
    ORDER_TYPE_SELL=1
    ORDER_FILLING_FOK=0
    ORDER_FILLING_IOC=1
    ORDER_TIME_GTC=0
    TRADE_ACTION_DEAL=1
    TRADE_RETCODE_DONE=10009
    def __init__(self, outcome="none"):
        self.outcome=outcome
        self.sent=[]
    def account_info(self): return SimpleNamespace(login=123,trade_mode=0, trade_allowed=True, equity=10000)
    def terminal_info(self): return SimpleNamespace(trade_allowed=True)
    def positions_get(self,symbol=None):
        if symbol is not None and self.sent and self.outcome == "done":
            request = self.sent[-1]
            return [SimpleNamespace(ticket=71,symbol=request["symbol"],magic=request["magic"],
                    type=request["type"],volume=request["volume"],sl=request["sl"],tp=request["tp"])]
        return []
    def orders_get(self,symbol=None): return []
    def history_orders_get(self,*a): return []
    def history_deals_get(self,*a): return []
    def symbol_info_tick(self,symbol): return SimpleNamespace(bid=100,ask=101,time=time.time())
    def symbol_info(self,symbol): return SimpleNamespace(point=0.01,trade_tick_size=0.01,trade_stops_level=0,filling_mode=1,digits=2,order_mode=63)
    def order_calc_profit(self,*args): return -50
    def order_check(self,request): return SimpleNamespace(retcode=0,comment="ok",request=SimpleNamespace(**request))
    def order_send(self,request):
        self.sent.append(request)
        if self.outcome=="raises":
            raise ConnectionError("transport reset")
        if self.outcome=="none": return None
        if self.outcome=="done":
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, deal=41, order=71, volume=.01)
        return SimpleNamespace(retcode=10010,deal=0,order=71,volume=.005)
    def last_error(self): return "injected failure"


def setup(tmp_path, monkeypatch, outcome):
    fake=FakeMT5(outcome)
    monkeypatch.setitem(sys.modules,"MetaTrader5",fake)
    module=importlib.import_module("mt5_agents.execution")
    monkeypatch.setattr(module,"mt5",fake)
    monkeypatch.setattr(module,"ROOT",tmp_path)
    memory=EventLogger(tmp_path / "audit.sqlite3")
    memory.record_signal(SignalSpec("s1", "XAUUSD", "BUY", datetime.now(timezone.utc),
                                   100,101,110,90,20,"APPROVED","m1","f1",{}))
    cfg=SimpleNamespace(login=123, mode="DEMO", max_open_positions=2,
                        max_quote_age_seconds=120, max_spread_atr_ratio=.2,
                        max_daily_loss_pct=1, magic=777)
    plan=SimpleNamespace(symbol="XAUUSD",side="BUY",price=101,sl=90,tp=110,
                         volume=.01,atr=10,risk_budget=100,start_equity=10000)
    return fake,module.ExecutionAgent(cfg),memory,plan


@pytest.mark.parametrize("outcome",["none","raises","partial"])
def test_uncertain_send_is_never_retried(tmp_path, monkeypatch, outcome):
    fake, agent, memory, plan=setup(tmp_path, monkeypatch, outcome)
    with pytest.raises(RuntimeError,match="(STOP|do not resubmit)"):
        agent.send(plan,memory=memory,signal_id="s1")
    assert len(fake.sent)==1
    assert fake.sent[0]["comment"]==correlation_comment("s1",123)
    assert (tmp_path / "STOP").exists()
    assert memory.unsettled_execution_intents()
    with pytest.raises(RuntimeError,match="STOP"):
        agent.send(plan,memory=memory,signal_id="s1")
    assert len(fake.sent)==1


def test_done_receipt_is_acknowledged_and_persisted(tmp_path,monkeypatch):
    fake,agent,memory,plan=setup(tmp_path, monkeypatch,"done")
    outcome=agent.send(plan,memory=memory,signal_id="s1")
    assert outcome["status"]=="BROKER_ACK_DONE_PROTECTED"
    assert len(fake.sent)==1
    assert memory.unsettled_execution_intents()==[]
    assert not (tmp_path / "STOP").exists()


def test_broker_ack_without_server_stops_forces_urgent_manual_stop(tmp_path,monkeypatch):
    fake, agent, memory, plan = setup(tmp_path,monkeypatch,"done")
    original = fake.positions_get
    def stripped(symbol=None):
        positions = original(symbol)
        if positions and symbol is not None:
            pos = positions[0]
            return [SimpleNamespace(**{**vars(pos), "sl":0.0})]
        return positions
    fake.positions_get = stripped
    with pytest.raises(RuntimeError, match="UNVERIFIED PROTECTION"):
        agent.send(plan,memory=memory,signal_id="s1")
    assert len(fake.sent)==1
    assert (tmp_path / "STOP").exists()
    with pytest.raises(RuntimeError,match="STOP"):
        agent.send(plan,memory=memory,signal_id="s1")
    assert len(fake.sent)==1


def test_symbol_without_both_server_stops_blocks_presend(tmp_path,monkeypatch):
    fake,agent,memory,plan = setup(tmp_path,monkeypatch,"done")
    old = fake.symbol_info
    def no_tp(symbol):
        info=old(symbol)
        info.order_mode=16
        return info
    fake.symbol_info=no_tp
    with pytest.raises(RuntimeError,match="BOTH server SL and TP"):
        agent.send(plan,memory=memory,signal_id="s1")
    assert fake.sent==[]


def test_broker_order_check_mismatched_stop_blocks_presend(tmp_path,monkeypatch):
    fake,agent,memory,plan=setup(tmp_path,monkeypatch,"done")
    fake.order_check=lambda request: SimpleNamespace(retcode=0,comment="ok",
            request=SimpleNamespace(**{**request,"sl":0.0}))
    with pytest.raises(RuntimeError,match="precheck changed/missed sl"):
        agent.send(plan,memory=memory,signal_id="s1")
    assert fake.sent==[]
