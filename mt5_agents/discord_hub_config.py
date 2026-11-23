"""Pure Discord configuration validation and conservative AI quota."""
from __future__ import annotations
import os
import threading
import json
from .ollama_local import validate_loopback_url
from .discord_agent_webhook import validate_agent_webhook
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone

def validate_hub_config(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    token = str(env.get('DISCORD_BOT_TOKEN','')).strip()
    if not token or token.lower().startswith(('replace_','your_')):
        raise ValueError('DISCORD_BOT_TOKEN missing; create a Discord application bot token')
    try:
        guild = int(str(env.get('DISCORD_GUILD_ID','')).strip())
        channel = int(str(env.get('DISCORD_CHANNEL_ID','')).strip())
    except ValueError as e:
        raise ValueError('DISCORD_GUILD_ID and DISCORD_CHANNEL_ID must be numeric') from e
    if guild <= 0 or channel <= 0:
        raise ValueError('Invalid Discord server/channel ID')
    ai_enabled = str(env.get('AGENT_AI_ENABLED','false')).lower()=='true'
    provider = str(env.get('AGENT_AI_PROVIDER','openai')).lower().strip()
    if provider not in ('openai', 'ollama'):
        raise ValueError('AGENT_AI_PROVIDER must be openai or ollama')
    ai_key = str(env.get('OPENAI_API_KEY','')).strip() if provider=='openai' else ''
    if ai_enabled and provider=='openai' and not ai_key:
        raise ValueError('AGENT_AI_ENABLED=true requires OPENAI_API_KEY for openai')
    model = str(env.get('AGENT_AI_MODEL','gpt-4.1-mini')).strip()
    if not model or any(x in model for x in ('/','\\','\n')):
        raise ValueError('Invalid AGENT_AI_MODEL')
    ollama_model = str(env.get('OLLAMA_MODEL','qwen3:4b')).strip()
    if not ollama_model or len(ollama_model)>128 or not all(c.isalnum() or c in '_:-.' for c in ollama_model):
        raise ValueError('Invalid OLLAMA_MODEL')
    ollama_url = validate_loopback_url(str(env.get('OLLAMA_URL','http://127.0.0.1:11434')))
    ollama_timeout = float(env.get('OLLAMA_TIMEOUT_SECONDS','120'))
    ollama_predict = int(env.get('OLLAMA_MAX_PREDICT','220'))
    if not 10 <= ollama_timeout <= 180:
        raise ValueError('OLLAMA_TIMEOUT_SECONDS must be between 10 and 180')
    if not 64 <= ollama_predict <= 512:
        raise ValueError('OLLAMA_MAX_PREDICT must be between 64 and 512')
    interval = int(env.get('AGENT_AUTO_DISCUSS_INTERVAL_SECONDS','3600'))
    max_daily = int(env.get('AGENT_MAX_DISCUSSIONS_PER_DAY','6'))
    manual_cooldown = int(env.get('AGENT_MANUAL_COOLDOWN_SECONDS','90'))
    if not 300 <= interval <= 86400:
        raise ValueError('AGENT_AUTO_DISCUSS_INTERVAL_SECONDS must be between 300 and 86400')
    if not 1 <= max_daily <= 96:
        raise ValueError('AGENT_MAX_DISCUSSIONS_PER_DAY must be between 1 and 96')
    if not 30 <= manual_cooldown <= 900:
        raise ValueError('AGENT_MANUAL_COOLDOWN_SECONDS must be between 30 and 900')
    named_agents = str(env.get('AGENT_DISTINCT_NAMES_ENABLED','false')).lower() == 'true'
    agent_webhook = str(env.get('DISCORD_AGENT_WEBHOOK_URL','')).strip()
    if named_agents:
        agent_webhook = validate_agent_webhook(agent_webhook)
    elif agent_webhook:
        # No unnoticed remote destination even when not sending anything.
        agent_webhook = validate_agent_webhook(agent_webhook)
    return {'token':token, 'guild':guild, 'channel':channel,
            'ai_enabled':ai_enabled,'ai_key':ai_key,'model':model,
            'provider':provider,'ollama_model':ollama_model,'ollama_url':ollama_url,
            'ollama_timeout':ollama_timeout,'ollama_predict':ollama_predict,
            'auto': str(env.get('AGENT_AUTO_DISCUSS_ENABLED','false')).lower()=='true',
            'auto_interval':interval,'max_daily':max_daily,
            'manual_cooldown':manual_cooldown,
            'named_agents':named_agents, 'agent_webhook':agent_webhook}


class Budget:
    """Daily, fail-closed limit. Optional JSON state survives Windows restarts.

    Files are journaled before making an API call. We do not auto-reset on
    corrupted quota state; an operator must inspect and repair it.
    """
    def __init__(self, limit: int, state_file: str | Path | None = None):
        self.limit = limit
        self.file = Path(state_file) if state_file else None
        self._date = ''
        self._count = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        now=datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            if self.file and self.file.exists():
                data=json.loads(self.file.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or not isinstance(data.get('count'), int) or not isinstance(data.get('date'), str):
                    raise RuntimeError('AI budget state invalid; FAIL CLOSED')
                if data['count'] < 0 or data['count'] > 10000:
                    raise RuntimeError('AI budget count invalid; FAIL CLOSED')
                self._date, self._count = data['date'],data['count']
            if now != self._date:
                self._date, self._count = now, 0
            if self._count >= self.limit:
                return False
            self._count += 1
            if self.file:
                self.file.parent.mkdir(parents=True,exist_ok=True)
                temp=self.file.with_name(self.file.name+'.'+uuid4().hex+'.tmp')
                try:
                    with temp.open('w',encoding='utf-8') as f:
                        json.dump({'date':self._date,'count':self._count},f)
                        f.flush()
                        import os
                        os.fsync(f.fileno())
                    temp.replace(self.file)
                finally:
                    temp.unlink(missing_ok=True)
            return True
