"""Ollama local provider: credentials never leave the read-only Discord hub."""
from datetime import datetime, timezone
import asyncio
import json
import time
from unittest.mock import Mock
import pytest

from mt5_agents.ollama_local import validate_loopback_url, OllamaLocalClient, LocalModelUnavailable
from mt5_agents.agent_discussion import request_roles_ollama, ROLES, validated_snapshot
from mt5_agents.discord_hub_config import validate_hub_config


@pytest.fixture
def base_config():
    return {'DISCORD_BOT_TOKEN':'fake-token','DISCORD_GUILD_ID':'123',
            'DISCORD_CHANNEL_ID':'456', 'AGENT_AI_ENABLED':'true',
            'AGENT_AI_PROVIDER':'ollama','OLLAMA_MODEL':'qwen3:4b'}


def test_ollama_config_needs_no_openai_key(base_config):
    result=validate_hub_config(base_config)
    assert result['provider']=='ollama'
    assert result['ai_key']==''
    assert result['ollama_model']=='qwen3:4b'
    assert result['ollama_url']=='http://127.0.0.1:11434'
    assert result['ollama_timeout']==120.0
    assert result['ollama_predict']==220


@pytest.mark.parametrize('bad',[ 'http://192.168.1.4:11434', 'http://8.8.8.8:11434',
     'https://127.0.0.1:11434', 'http://127.0.0.1:11434/foo',
     'http://foo@localhost:11434', 'http://localhost:11434?x=1',
     'http://localhost:11434#foo', 'http://localhost', 'file:///tmp/secret',
     'http://localhost:0', 'http://localhost:99999'])
def test_loopback_only_disallows_remote_or_redirectable_routes(bad):
    with pytest.raises(ValueError,match='local-only'):
        validate_loopback_url(bad)


def test_limited_local_payload_and_no_proxy():
    client=OllamaLocalClient('http://127.0.0.1:11434')
    sent=[]
    class Resp:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self,n): return json.dumps({'done':True,'message':{'content':'نعم، محلي'}}).encode()
    def fake_open(request,timeout):
        sent.append((request,timeout))
        return Resp()
    client.opener.open=fake_open
    result=client.chat(model='qwen3:4b',instructions='Read only',
                       data={'symbol':'XAUUSD'},timeout=120,max_predict=220)
    assert result=='نعم، محلي'
    request,timeout=sent[0]
    assert request.full_url=='http://127.0.0.1:11434/api/chat'
    body=json.loads(request.data.decode('utf8'))
    assert body['stream'] is False and body['think'] is False
    assert body['options']['num_predict']==220
    assert body['options']['num_ctx']==3072
    assert body['messages'][0]['role']=='system'
    assert timeout==120


def test_four_distinct_roles_no_secret_data():
    snapshot={'symbol':'XAUUSD','observed_utc':datetime.now(timezone.utc).isoformat(),
              'bid':3548.31,'ask':3548.46,'atr':3.1,'rsi':51.1,'spread':.15,
              'technical_side':'HOLD','technical_reason':'No signal',
              'MT5_PASSWORD':'secret'}
    safe=validated_snapshot(snapshot,time.time())
    class Fake:
        def __init__(self): self.calls=[]
        def chat(self, **kwargs):
            self.calls.append(kwargs)
            return 'No signal, data is partial.'
    mock=Fake()
    result=request_roles_ollama(snapshot=safe,model='qwen3:4b',
                                url='http://127.0.0.1:11434',question='كيف الحالة؟',
                                timeout=120,max_predict=220,client=mock)
    assert [role for role,_ in result]==[r for r,_ in ROLES]
    assert len(mock.calls)==4
    assert all('secret' not in str(c) for c in mock.calls)
    assert all(c['timeout']==120 and c['max_predict']==220 for c in mock.calls)
    assert mock.calls[1]['data']['previous_agent_notes'][0]['role']=='MARKET'


def test_model_empty_fails_closed():
    class Empty:
        def chat(self,**kw): return ''
    from datetime import datetime, timezone
    snap={'symbol':'XAUUSD','observed_utc':datetime.now(timezone.utc).isoformat(),
          'bid':3548.31,'ask':3548.46,'atr':3.1,'rsi':51.1,'spread':.15,
          'technical_side':'HOLD','technical_reason':'No signal'}
    with pytest.raises(Exception,match='empty'):
        request_roles_ollama(snapshot=snap,model='qwen3:4b',client=Empty())


def test_config_bounds_reject_excessive_compute(base_config):
    for name,value in [('OLLAMA_TIMEOUT_SECONDS','1000'),
                       ('OLLAMA_MAX_PREDICT','100000'),
                       ('OLLAMA_URL','https://external.example.com')]:
        with pytest.raises(ValueError):
            validate_hub_config({**base_config,name:value})


def test_provider_switch_still_supports_existing_openai_guard(base_config):
    cfg={**base_config,'AGENT_AI_PROVIDER':'openai'}
    with pytest.raises(ValueError,match='OPENAI_API_KEY'):
        validate_hub_config(cfg)


def test_ollama_preflight_uses_real_local_generation_when_enabled(monkeypatch,capsys):
    from mt5_agents import ollama_preflight as module
    monkeypatch.setenv('AGENT_AI_PROVIDER','ollama')
    monkeypatch.setenv('AGENT_AI_ENABLED','true')
    monkeypatch.setenv('OLLAMA_MODEL','qwen3:4b')
    class Fake:
        def __init__(self,url): pass
        def installed_models(self): return ['qwen3:4b']
        def chat(self,**kwargs):
            assert kwargs['max_predict']==64
            return 'نعم'
    monkeypatch.setattr(module,'OllamaLocalClient',Fake)
    assert module.main()==0
    assert json.loads(capsys.readouterr().out)['inference_test'] is True


def test_ollama_helper_sanitizes_unscreened_snapshot():
    snap={'symbol':'XAUUSD','observed_utc':datetime.now(timezone.utc).isoformat(),
          'bid':3548.31,'ask':3548.46,'atr':3.1,'rsi':51.1,'spread':.15,
          'technical_side':'HOLD','technical_reason':'No signal',
          'MT5_PASSWORD':'must-not-leak'}
    class Fake:
        def __init__(self): self.payloads=[]
        def chat(self,**kw): self.payloads.append(str(kw)); return 'مراجعة فقط'
    fake=Fake()
    request_roles_ollama(snapshot=snap,model='qwen3:4b',client=fake)
    assert len(fake.payloads)==4
    assert all('must-not-leak' not in p for p in fake.payloads)


def test_http_loopback_roundtrip_without_external_provider():
    # Local mock HTTP server exercises real stdlib HTTP transport, not model inference.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, None))
            data={'models':[{'name':'qwen3:4b'}]}
            body=json.dumps(data).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,body))
            payload={'done':True,'message':{'role':'assistant','content':'Ollama fake loopback works'}}
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.end_headers(); self.wfile.write(json.dumps(payload).encode())
        def log_message(self,*args): pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        client=OllamaLocalClient(f'http://127.0.0.1:{server.server_port}')
        assert client.installed_models()==['qwen3:4b']
        response=client.chat(model='qwen3:4b',instructions='read only',data={'symbol':'XAUUSD'})
        assert response=='Ollama fake loopback works'
        assert [p for p,_ in calls]==['/api/tags','/api/chat']
        assert calls[1][1]['stream'] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
