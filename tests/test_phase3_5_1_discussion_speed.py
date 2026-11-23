"""Progressive publication and distinct-role prompts, without Ollama or Discord network."""
from mt5_agents.agent_discussion import request_roles_ollama, ROLES


def test_ollama_progresses_after_each_role_and_carries_prior_discussion():
    events = []
    calls = []
    class Fake:
        def chat(self, **kwargs):
            role = next(r for r,_ in ROLES if f'التحليلي {r} ' in kwargs['instructions'])
            calls.append((role, kwargs['data']))
            return {'MARKET':'رصد: تحقق من جودة المصدر.', 'QUANT':'فرضية: اختبر خارج العينة.',
                    'RISK':'اعتراض: فروقات السبريد مهمة.', 'COORDINATOR':'خلاصة: نحتاج بيانات أكثر.'}[role]
    result = request_roles_ollama(snapshot=None, offline=True, model='qwen2.5:7b',
                                  client=Fake(), on_role=lambda role, text: events.append((role,text)))
    assert events == result
    assert [r for r,_ in result] == [r for r,_ in ROLES]
    assert calls[0][1]['previous_agent_notes'] == []
    assert calls[1][1]['previous_agent_notes'][0]['role'] == 'MARKET'
    assert calls[2][1]['previous_agent_notes'][-1]['role'] == 'QUANT'
    assert calls[3][1]['previous_agent_notes'][-1]['role'] == 'RISK'


def test_non_iteractive_default_remains_backward_compatible():
    class Fake:
        def chat(self, **kwargs): return 'نقاش مختصر'
    assert len(request_roles_ollama(snapshot=None,offline=True,model='qwen2.5:7b', client=Fake())) == 4


def test_offline_instructions_push_distinct_outputs():
    seen={}
    class Fake:
        def chat(self, **kwargs):
            for role,_ in ROLES:
                if f'التحليلي {role} ' in kwargs['instructions']:
                    seen[role] = kwargs['instructions']
                    return role
            raise AssertionError('unknown role')
    request_roles_ollama(snapshot=None,offline=True,model='qwen2.5:7b',client=Fake())
    assert 'جودة البيانات' in seen['MARKET']
    assert 'فرضية' in seen['QUANT']
    assert 'انزلاق' in seen['RISK']
    assert 'اتفاق' in seen['COORDINATOR'] or 'الخلاف' in seen['COORDINATOR']


def test_hub_source_uses_worker_thread_coro_bridge():
    from pathlib import Path
    hub = (Path(__file__).resolve().parents[1]/'mt5_agents'/'discord_hub.py').read_text(encoding='utf8')
    assert 'asyncio.run_coroutine_threadsafe' in hub
    assert 'on_role=self._role_publisher_from_worker()' in hub
    assert "if self.config['provider'] != 'ollama':" in hub
