"""PROVISIONAL broker-offset mode never authorizes archives or trades."""
from datetime import datetime, timezone
from types import SimpleNamespace
import sys
import pandas as pd
import pytest

from mt5_agents.observation_timebase import ObservationTimebase
from mt5_agents.discord_observer import Observer, quote_health
from mt5_agents.agent_discussion import validated_snapshot


def cfg(mode='DRY_RUN', server='MetaQuotes-Demo'):
    return SimpleNamespace(mode=mode, server=server,
                           symbols=('XAUUSD',), timeframe='M15',
                           max_quote_age_seconds=120,
                           max_bar_age_seconds=120,
                           ai_model='unused')


def env(**overrides):
    result = {'MT5_OBSERVER_TIMEBASE_MODE':'PROVISIONAL_BROKER_OFFSET',
              'MT5_OBSERVER_TIMEBASE_SERVER':'MetaQuotes-Demo',
              'MT5_OBSERVER_OFFSET_SECONDS':'7200',
              'MT5_OBSERVER_TIMEBASE_ACK':'I_ACCEPT_OBSERVATION_ONLY_NO_ARCHIVE'}
    result.update(overrides)
    return result


def test_default_strict_utc_without_config():
    x=ObservationTimebase.from_env(cfg(),{})
    assert x.offset_seconds==0 and x.archive_allowed and not x.provisional


def test_explicit_opt_in_only_for_matching_demo_observer():
    x=ObservationTimebase.from_env(cfg(),env())
    assert x.offset_seconds==7200
    assert not x.archive_allowed and x.provisional
    assert x.to_observed_utc_s(1_800_000_000+7200)==1_800_000_000


@pytest.mark.parametrize('bad', [
    {'MT5_OBSERVER_TIMEBASE_ACK':''},
    {'MT5_OBSERVER_TIMEBASE_ACK':'YES'},
    {'MT5_OBSERVER_TIMEBASE_SERVER':'WrongBroker'},
    {'MT5_OBSERVER_TIMEBASE_SERVER':''},
    {'MT5_OBSERVER_OFFSET_SECONDS':'bad'},
    {'MT5_OBSERVER_OFFSET_SECONDS':'0'},
    {'MT5_OBSERVER_OFFSET_SECONDS':'7500'},
    {'MT5_OBSERVER_OFFSET_SECONDS':'99999999'},
    {'MT5_OBSERVER_TIMEBASE_MODE':'UNKNOWN'},
])
def test_bad_configuration_fail_closed(bad):
    with pytest.raises((RuntimeError,ValueError)):
        ObservationTimebase.from_env(cfg(),env(**bad))


def test_fail_closed_for_non_dry_run():
    with pytest.raises(RuntimeError, match='DRY_RUN'):
        ObservationTimebase.from_env(cfg(mode='DEMO'),env())


def test_partial_rollout_strict_with_offset_refused():
    with pytest.raises(RuntimeError):
        ObservationTimebase.from_env(cfg(),{'MT5_OBSERVER_TIMEBASE_MODE':'STRICT_UTC',
                                             'MT5_OBSERVER_OFFSET_SECONDS':'7200'})


def test_future_raw_quote_becomes_recent_observation_only():
    now=1_800_000_000
    tick=SimpleNamespace(time_msc=(now+7197)*1000, time=now+7197,
                         bid=3600.0,ask=3600.15)
    assert quote_health(tick,now,120)[0] is False
    assert quote_health(tick,now,120,offset_seconds=7200)==(True,'FRESH')
    old=SimpleNamespace(time_msc=(now+7200-300)*1000,bid=3600,ask=3600.15)
    assert not quote_health(old,now,120,offset_seconds=7200)[0]
    wrong_offset=SimpleNamespace(time_msc=(now+10800)*1000,bid=3600,ask=3600.15)
    assert not quote_health(wrong_offset,now,120,offset_seconds=7200)[0]
    invalid=SimpleNamespace(time_msc=(now+7200)*1000,bid=0,ask=0)
    assert not quote_health(invalid,now,120,offset_seconds=7200)[0]


def test_provisional_market_observer_does_not_touch_ticks_or_manifest(monkeypatch):
    now=1_800_000_000 // 900 * 900
    policy=ObservationTimebase.from_env(cfg(),env())
    class MT5:
        def symbol_select(self,symbol,visible): return True
        def symbol_info_tick(self,symbol):
            return SimpleNamespace(time_msc=(now+7199)*1000,time=now+7199,
                                   bid=3600.0,ask=3600.15)
    class Archive:
        def latest_end(self,s): raise AssertionError('never read archive in provisional mode')
        def collect(self,*a): raise AssertionError('never archive provisional data')
    class Notifier:
        messages=[]
        def send(self,s): self.messages.append(s)
    monkeypatch.setitem(sys.modules, 'mt5_agents.market',
                        SimpleNamespace(MarketDataAgent=lambda tf: SimpleNamespace(seconds=900)))
    obs=Observer(cfg(),MT5(),Notifier(),Archive(),timebase=policy,now=lambda:now)
    obs.market=SimpleNamespace(seconds=900,candles=lambda s: pd.DataFrame([
        {'time': now-900+7200},
    ]))
    obs.quant=SimpleNamespace(decide=lambda s,b:SimpleNamespace(
        side='HOLD',rsi=53,atr=3,reason='No signal'))
    obs._symbol_cycle('XAUUSD',datetime.fromtimestamp(now,timezone.utc))
    assert obs.snapshots['XAUUSD']['timebase_status']=='PROVISIONAL_BROKER_OFFSET_NO_ARCHIVE'
    assert any('TIMEBASE_PROVISIONAL' in msg for msg in Notifier.messages)
    safe=validated_snapshot(obs.snapshots['XAUUSD'],now)
    assert 'timebase_warning' in safe
    assert 'MT5_LOGIN' not in safe


def test_non_dry_run_never_constructs_provisional_observer(monkeypatch):
    monkeypatch.setitem(sys.modules, 'mt5_agents.market',
                        SimpleNamespace(MarketDataAgent=lambda tf: SimpleNamespace(seconds=900)))
    with pytest.raises(RuntimeError):
        Observer(cfg(mode='DEMO'),object(),object(),object(),
                 timebase=ObservationTimebase('PROVISIONAL_BROKER_OFFSET',7200))


def test_inconsistent_raw_time_and_time_msc_refused():
    now=1_800_000_000
    tick=SimpleNamespace(time=now+7200+300, time_msc=(now+7200)*1000,
                         bid=3600.0, ask=3600.15)
    assert quote_health(tick,now,120,offset_seconds=7200)==(
        False,'INCONSISTENT_QUOTE_TIMESTAMPS')


def test_provisional_mode_disables_collector_cli():
    from mt5_agents.observation_timebase import require_unshifted_archive_timebase
    with pytest.raises(RuntimeError, match='blocked'):
        require_unshifted_archive_timebase({'MT5_OBSERVER_TIMEBASE_MODE':'PROVISIONAL_BROKER_OFFSET'})
    require_unshifted_archive_timebase({})


def test_provisional_environment_disables_execution(monkeypatch):
    from mt5_agents.settings import Settings
    monkeypatch.setenv('MT5_LOGIN','1234567')
    monkeypatch.setenv('MT5_PASSWORD','fake-test-only')
    monkeypatch.setenv('MT5_SERVER','MetaQuotes-Demo')
    monkeypatch.setenv('MT5_OBSERVER_TIMEBASE_MODE','PROVISIONAL_BROKER_OFFSET')
    monkeypatch.setenv('MODE','DEMO')
    monkeypatch.setenv('AI_VETO_ENABLED','false')
    with pytest.raises(ValueError, match='forbids DEMO/LIVE'):
        Settings.load()
