"""Phase 3.5: separate agent identities, Arabic and conservative lock recovery."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import types
import pytest

from mt5_agents.discord_agent_webhook import (
    ROLE_NAMES_AR, NamedAgentPoster, validate_agent_webhook,
)
from mt5_agents.agent_discussion import request_roles_ollama, ROLES
from mt5_agents.discord_hub_config import validate_hub_config
from mt5_agents.discord_hub import Hub
from mt5_agents.discord_lock_health import inspect_lock, clear_stale_lock

URL='https://discord.com/api/webhooks/123456789/token_letters_ABC-09'


def config(**kw):
    result={'DISCORD_BOT_TOKEN':'fake', 'DISCORD_GUILD_ID':'123',
            'DISCORD_CHANNEL_ID':'456', 'AGENT_AI_ENABLED':'true',
            'AGENT_AI_PROVIDER':'ollama','OLLAMA_MODEL':'qwen2.5:7b'}
    result.update(kw)
    return result


def test_four_distinct_arabic_names():
    assert set(ROLE_NAMES_AR) == {role for role,_ in ROLES}
    assert len(set(ROLE_NAMES_AR.values())) == 4
    assert all(any('\u0600' <= ch <= '\u06ff' for ch in name) for name in ROLE_NAMES_AR.values())


@pytest.mark.parametrize('bad', [
    'http://discord.com/api/webhooks/123/x',
    'https://example.com/api/webhooks/123/x',
    'https://discord.com.evil.org/api/webhooks/123/x',
    'https://discord.com/api/webhooks/123/x?wait=true',
    'https://discord.com/api/webhooks/123/x#hash',
    'https://discord.com/api/webhooks/abc/x',
    '',
])
def test_webhook_rejects_non_discord_and_unsafe_urls(bad):
    with pytest.raises(ValueError):
        validate_agent_webhook(bad)


def test_named_config_requires_valid_webhook_and_new_quota_bounds():
    with pytest.raises(ValueError,match='DISCORD_AGENT_WEBHOOK_URL'):
        validate_hub_config(config(AGENT_DISTINCT_NAMES_ENABLED='true'))
    result=validate_hub_config(config(AGENT_DISTINCT_NAMES_ENABLED='true',
        DISCORD_AGENT_WEBHOOK_URL=URL, AGENT_MAX_DISCUSSIONS_PER_DAY='48',
        AGENT_AUTO_DISCUSS_INTERVAL_SECONDS='600', AGENT_MANUAL_COOLDOWN_SECONDS='90'))
    assert result['named_agents'] and result['agent_webhook']==URL
    assert result['max_daily']==48 and result['auto_interval']==600
    assert result['manual_cooldown']==90


@pytest.mark.parametrize('kv', [
    {'AGENT_MAX_DISCUSSIONS_PER_DAY':'10000'},
    {'AGENT_MAX_DISCUSSIONS_PER_DAY':'0'},
    {'AGENT_AUTO_DISCUSS_INTERVAL_SECONDS':'1'},
    {'AGENT_MANUAL_COOLDOWN_SECONDS':'0'},
])
def test_discussion_limits_cannot_be_disabled(kv):
    with pytest.raises(ValueError):
        validate_hub_config(config(**kv))


def test_named_webhook_posts_as_four_independent_names_without_mentions(monkeypatch):
    sent=[]
    class FakeSession:
        def __init__(self,**kw): self.kw=kw
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
    class FakeTimeout:
        def __init__(self,total): self.total=total
    monkeypatch.setitem(sys.modules,'aiohttp',types.SimpleNamespace(
        ClientTimeout=FakeTimeout,ClientSession=FakeSession))
    class FakeHook:
        async def fetch(self): return types.SimpleNamespace(channel_id=456, guild_id=123)
        async def send(self,content,**kw): sent.append((content,kw))
    class FakeWebHook:
        @staticmethod
        def from_url(url,**kwargs):
            assert url == URL
            return FakeHook()
    class FakeAllowed:
        @staticmethod
        def none(): return 'NO_MENTIONS'
    discord=types.SimpleNamespace(Webhook=FakeWebHook,AllowedMentions=FakeAllowed)
    async def run():
        poster=NamedAgentPoster(URL,discord,expected_channel=456,expected_guild=123)
        for role,_ in ROLES:
            await poster.send(role,'نقاش قصير')
    asyncio.run(run())
    assert len(sent)==4
    assert [kw['username'] for _,kw in sent] == list(ROLE_NAMES_AR.values())
    assert all(kw['allowed_mentions']=='NO_MENTIONS' for _,kw in sent)
    assert all(kw['wait'] is True for _,kw in sent)


def test_hub_publishes_role_separately():
    observed=[]
    class Poster:
        async def send(self,role,message): observed.append((role,message))
    class FakeHub:
        named_poster=Poster()
    asyncio.run(Hub._publish_roles(FakeHub(), [('MARKET','سوق'),('RISK','مخاطر')]))
    assert observed == [('MARKET','سوق'),('RISK','مخاطر')]


def test_offline_roundtable_uses_no_live_quotes_and_arabic_prompt():
    calls=[]
    class Fake:
        def chat(self,**kw):
            calls.append(kw)
            return 'هذه مناقشة بحثية دون أسعار لحظية.'
    results=request_roles_ollama(snapshot=None,offline=True,client=Fake(),
                                  model='qwen2.5:7b',question='اشرح فجوة البيانات')
    assert len(results)==4
    assert all(c['data']['market_snapshot']['status']=='NO_LIVE_MARKET_DATA' for c in calls)
    assert all('بالعربية' in c['instructions'] and 'تداول' in c['instructions'] for c in calls)
    assert all('bid' not in c['data']['market_snapshot'] for c in calls)
    with pytest.raises(ValueError):
        request_roles_ollama(snapshot={'symbol':'XAUUSD'},offline=True,
                             client=Fake(), model='qwen2.5:7b')


def test_lock_inspection_does_not_delete_live_lock(tmp_path):
    path=tmp_path/'discord_hub.lock'
    path.write_text('pid=123 at=2026-11-22T16:00:00+00:00\n')
    assert inspect_lock(path, pid_exists=lambda pid: True)['status']=='ACTIVE_PID'
    assert clear_stale_lock(path, pid_exists=lambda pid: True)['status']=='REFUSED'
    assert path.exists()


def test_lock_cleanup_requires_explicit_stale_and_valid_format(tmp_path):
    path=tmp_path/'discord_hub.lock'
    path.write_text('pid=123 at=2026-11-22T16:00:00+00:00\n')
    assert inspect_lock(path,pid_exists=lambda pid:False)['status']=='STALE_CANDIDATE'
    assert path.exists()  # inspection is read-only
    assert clear_stale_lock(path,pid_exists=lambda pid:False)['status']=='REMOVED_STALE'
    assert not path.exists()
    path.write_text('unexpected lock content')
    assert clear_stale_lock(path,pid_exists=lambda pid:False)['status']=='REFUSED'
    assert path.exists()


def test_webhook_cannot_post_into_different_discord_channel(monkeypatch):
    class Session:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*kw): pass
    class FakeHook:
        async def fetch(self): return types.SimpleNamespace(channel_id=999,guild_id=123)
        async def send(self,*a,**kw): raise AssertionError('Must never send')
    class Webhook:
        @staticmethod
        def from_url(url,**kw): return FakeHook()
    class Allowed:
        @staticmethod
        def none(): return None
    class Timeout:
        def __init__(self,total): pass
    monkeypatch.setitem(sys.modules,'aiohttp',types.SimpleNamespace(
        ClientSession=Session,ClientTimeout=Timeout))
    sender=NamedAgentPoster(URL,types.SimpleNamespace(Webhook=Webhook,AllowedMentions=Allowed),
                            expected_channel=456,expected_guild=123)
    with pytest.raises(RuntimeError, match='wrong Discord channel'):
        asyncio.run(sender.send('MARKET','testing'))
