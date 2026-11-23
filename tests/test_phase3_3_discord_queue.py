"""Regression: Discord network slow/offline cannot block the MT5 observer."""
import asyncio
from queue import Queue
from time import monotonic
import pytest
from mt5_agents.discord_hub import DiscordChannelNotifier, Hub


class FakeLoop:
    def __init__(self):
        self.closed=False
        self.calls=[]
    def is_closed(self): return self.closed
    def call_soon_threadsafe(self, callback):
        self.calls.append(callback)  # deliberately never dispatch: simulated hung gateway

class FakeSignal:
    def set(self): pass

class FakeHub:
    def __init__(self, size):
        self.notification_queue=Queue(maxsize=size)
        self.notification_signal=FakeSignal()


def test_notifier_returns_without_waiting_for_discord():
    hub=FakeHub(size=2)
    loop=FakeLoop()
    notifier=DiscordChannelNotifier(hub,loop)
    before=monotonic()
    notifier.send('hello')
    assert monotonic()-before < 0.25
    assert hub.notification_queue.get_nowait()=='hello'
    assert len(loop.calls)==1


def test_notification_queue_is_bounded_and_fails_fast():
    hub=FakeHub(size=1)
    notifier=DiscordChannelNotifier(hub,FakeLoop())
    notifier.send('first')
    before=monotonic()
    with pytest.raises(RuntimeError, match='queue full'):
        notifier.send('second')
    assert monotonic()-before < 0.25
    assert hub.notification_queue.qsize()==1


def test_closed_loop_refuses_to_enqueue():
    hub=FakeHub(size=1)
    loop=FakeLoop(); loop.closed=True
    with pytest.raises(RuntimeError, match='event loop unavailable'):
        DiscordChannelNotifier(hub,loop).send('x')
    assert hub.notification_queue.empty()


def test_notification_worker_delivers_without_observer_thread():
    # asyncio consumer sends with timeout; use a fake Hub without a live Discord client.
    from threading import Event
    class Runtime:
        stop=Event()
    class Runner:
        runtime=Runtime()
        notification_queue=Queue(maxsize=2)
        notification_signal=asyncio.Event()
        messages=[]
        async def post(self, text): self.messages.append(text); self.runtime.stop.set()
    async def run():
        runner=Runner()
        runner.notification_queue.put_nowait('test-message')
        runner.notification_signal.set()
        await asyncio.wait_for(Hub._notification_worker(runner),timeout=1.0)
        assert runner.messages==['test-message']
        assert runner.notification_queue.empty()
    asyncio.run(run())


def test_llm_client_has_ten_second_timeout_and_output_cap(monkeypatch):
    """Four LLM role calls must be bounded even if the SDK defaults change."""
    import sys
    from types import SimpleNamespace
    from mt5_agents.agent_discussion import request_roles
    constructor = []
    calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructor.append(kwargs)
            self.responses = SimpleNamespace(create=self.create)
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text='مراجعة تجريبية دون توصية تداول')

    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=FakeOpenAI))
    snapshot = {'symbol':'XAUUSD','observed_utc':'2026-11-20T18:00:00+00:00',
                'bid':3548.31,'ask':3548.46,'atr':2.0,'rsi':50.0,
                'spread':0.15,'technical_side':'HOLD','technical_reason':'test'}
    request_roles(snapshot=snapshot,model='test-model',api_key='fake-local-secret')
    assert constructor[0]['timeout'] == 10.0
    assert constructor[0]['max_retries'] == 0
    assert len(calls) == 4
    assert all(c['max_output_tokens'] == 330 for c in calls)
