"""The true pre-send boundary must not call the broker after failed audit."""
import importlib
from types import SimpleNamespace
import sys
import time

import pytest

from mt5_agents.memory.db import PersistenceError


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_TIME_GTC = 0

    def __init__(self):
        self.send_calls = 0
        self.account = SimpleNamespace(login=123, trade_mode=0, trade_allowed=True, equity=10000)

    def account_info(self): return self.account
    def terminal_info(self): return SimpleNamespace(trade_allowed=True)
    def positions_get(self, symbol=None): return []
    def orders_get(self, symbol=None): return []
    def symbol_info_tick(self, symbol): return SimpleNamespace(bid=100, ask=101, time=time.time())
    def symbol_info(self, symbol):
        return SimpleNamespace(point=0.01,trade_tick_size=0.01,trade_stops_level=0,filling_mode=1,digits=2,order_mode=63)
    def order_calc_profit(self,*a): return -50
    def order_check(self,request): return SimpleNamespace(retcode=0,comment="ok",request=SimpleNamespace(**request))
    def order_send(self,request):
        self.send_calls += 1
        raise AssertionError("BROKER ORDER MUST NOT BE SENT")


class BrokenAudit:
    def record_execution_intent(self,*a,**kw):
        raise PersistenceError("simulated locked disk")


@pytest.mark.parametrize("error", [PersistenceError("sqlite busy"), RuntimeError("duplicate intent")])
def test_order_send_is_never_called_after_uncommitted_intent(monkeypatch,tmp_path,error):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules,"MetaTrader5",fake)
    module = importlib.import_module("mt5_agents.execution")
    monkeypatch.setattr(module,"mt5",fake)
    monkeypatch.setattr(module,"ROOT",tmp_path)
    cfg = SimpleNamespace(
        login=123, mode="DEMO", max_open_positions=2,
        max_quote_age_seconds=120, max_spread_atr_ratio=0.2,
        max_daily_loss_pct=1, magic=12345,
    )
    plan = SimpleNamespace(
        symbol="XAUUSD",side="BUY",price=101.0,sl=90.0,tp=110.0,
        volume=0.01,atr=10.0,risk_budget=100.0,start_equity=10000,
    )
    class Failing:
        def unsettled_execution_intents(self):
            return []
        def record_execution_intent(self,*args,**kwargs):
            raise error
    with pytest.raises(type(error)):
        module.ExecutionAgent(cfg).send(plan, memory=Failing(), signal_id="s1")
    assert fake.send_calls == 0
    assert (tmp_path / "STOP").exists()
