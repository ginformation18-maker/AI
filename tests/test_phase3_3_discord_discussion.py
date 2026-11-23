from datetime import datetime, timezone
import time
import pytest

from mt5_agents.agent_discussion import (validated_snapshot, DiscussionUnavailable,
                                          request_roles, ROLES)


def sample():
    return {'symbol':'XAUUSD', 'observed_utc':datetime.now(timezone.utc).isoformat(),
            'bid':3548.31,'ask':3548.46,'spread':0.15,'rsi':53.0,'atr':3.1,
            'technical_side':'HOLD','technical_reason':'No crossover',
            'MT5_LOGIN':'SHOULD NEVER SEND','PASSWORD':'NO'}


def test_discussion_snapshot_sanitized():
    x=validated_snapshot(sample(),time.time())
    assert x['symbol']=='XAUUSD'
    assert 'MT5_LOGIN' not in x and 'PASSWORD' not in x
    assert x['ask']-x['bid']>0


def test_stale_quote_refused():
    x=sample()
    x['observed_utc']='2020-01-01T00:00:00+00:00'
    with pytest.raises(DiscussionUnavailable):
        validated_snapshot(x,time.time())


def test_bad_quote_refused():
    x=sample(); x['ask']=0
    with pytest.raises(DiscussionUnavailable):
        validated_snapshot(x,time.time())


def test_no_api_key_no_fake_ai():
    with pytest.raises(DiscussionUnavailable,match='OPENAI_API_KEY'):
        request_roles(snapshot=sample(),model='test',api_key='')


def test_separate_llm_calls_for_each_role():
    class Response:
        output_text='تحليل مختصر مع ذكر حدود البيانات.'
    class Responses:
        def __init__(self): self.calls=[]
        def create(self,**kw): self.calls.append(kw); return Response()
    class Client:
        def __init__(self): self.responses=Responses()
    client=Client()
    r=request_roles(snapshot=validated_snapshot(sample(),time.time()),model='test',
                    api_key='dummy-local-token',client=client)
    assert [a for a,b in r]==[x[0] for x in ROLES]
    assert len(client.responses.calls)==4
    assert all(call['store'] is False for call in client.responses.calls)
    assert all('MT5_LOGIN' not in call['input'] for call in client.responses.calls)


def test_config_fields_without_discord_dependency():
    from mt5_agents.discord_hub_config import validate_hub_config, Budget
    conf={'DISCORD_BOT_TOKEN':'local-secret', 'DISCORD_GUILD_ID':'123',
          'DISCORD_CHANNEL_ID':'456','AGENT_AI_ENABLED':'false',
          'AGENT_AUTO_DISCUSS_ENABLED':'false'}
    result=validate_hub_config(conf)
    assert not result['ai_enabled'] and not result['auto']
    with pytest.raises(ValueError):
        validate_hub_config({**conf,'AGENT_AI_ENABLED':'true'})
    budget=Budget(2)
    assert budget.acquire() and budget.acquire() and not budget.acquire()


def test_budget_survives_restart(tmp_path):
    from mt5_agents.discord_hub_config import Budget
    path=tmp_path/'quota.json'
    assert Budget(1,path).acquire()
    assert not Budget(1,path).acquire()
